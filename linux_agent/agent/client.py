"""MCP client plus a small, explicit Plan -> Act -> Observe agent loop."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from groq import APIConnectionError, APITimeoutError, Groq, InternalServerError, RateLimitError
from mcp import ClientSession
from mcp.types import Tool

from ..audit import AuditLog
from ..security import redact_obj, truncate_bytes

WRITE_TOOL_NAMES = {
    "service_action",
    "nginx_set_parameter",
    "nginx_rollback",
}


class LLMError(RuntimeError):
    """The language model could not produce a response."""


@dataclass
class AgentSettings:
    model: str = "openai/gpt-oss-20b"
    max_steps: int = 8
    max_tool_output_chars: int = 12000
    temperature: float = 0.1
    auto_approve: bool = False
    show_tool_output: bool = True
    system_prompt: str = ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Turn:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolApproval:
    allowed: bool
    reason: str = ""
    always: bool = False


ApprovalCallback = Callable[[Tool, dict[str, Any]], Awaitable[ToolApproval]]
PrintCallback = Callable[[str], None]


class GroqLLM:
    def __init__(self, model: str, temperature: float, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError(
                "GROQ_API_KEY is not set. Add it to .env or export it in the environment."
            )
        self.client = Groq(api_key=key)
        self.model = model
        self.temperature = temperature

    def _complete_sync(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
        )
        message = response.choices[0].message
        calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for call in (message.tool_calls or [])
        ]
        return Turn(content=message.content or "", tool_calls=calls)

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._complete_sync, messages, tools)
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 ** (attempt + 1))
            except Exception as exc:
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        raise LLMError(f"Groq API failed after retries: {last_error}")


class MCPAgent:
    """A deliberately small agent loop with explicit human approval."""

    def __init__(
        self,
        session: ClientSession,
        llm: GroqLLM,
        settings: AgentSettings,
        *,
        approval: ApprovalCallback | None = None,
        printer: PrintCallback = print,
        session_log: AuditLog | None = None,
    ) -> None:
        self.session = session
        self.llm = llm
        self.settings = settings
        self.approval = approval
        self.printer = printer
        self.session_log = session_log
        self.tools: list[Tool] = []
        self.tool_map: dict[str, Tool] = {}
        self.messages: list[dict[str, Any]] = []
        self.always_approved: set[str] = set()
        self.last_turns: list[dict[str, Any]] = []

    async def load_tools(self) -> list[Tool]:
        result = await self.session.list_tools()
        self.tools = list(result.tools)
        self.tool_map = {tool.name: tool for tool in self.tools}
        self.reset()
        return self.tools

    def reset(self) -> None:
        system = self.settings.system_prompt or (
            "You are Linux Agent, a careful Linux system administrator assistant. "
            "Use the MCP tools to inspect the host before making claims. "
            "For any configuration change: inspect first, use dry_run=true, explain the diff, "
            "and only apply after explicit approval. Never invent command output. "
            "Mutating tools are allow-listed and audited. Answer concisely in Markdown."
        )
        tool_lines = "\n".join(
            f"- {tool.name}: {tool.description or ''}".strip() for tool in self.tools
        )
        self.messages = [
            {
                "role": "system",
                "content": system + "\n\nAvailable tools:\n" + (tool_lines or "(none)"),
            }
        ]

    # ------------------------------------------------------------------ tools
    def _schema(self, tool: Tool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }

    def is_mutating(self, tool: Tool) -> bool:
        annotations = tool.annotations
        if tool.name in WRITE_TOOL_NAMES:
            return True
        if annotations is not None and annotations.readOnlyHint is not None:
            return not annotations.readOnlyHint
        # Unknown tools without annotations are treated as mutating.
        return True

    def needs_approval(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        if not self.is_mutating(tool):
            return False
        if tool.name in WRITE_TOOL_NAMES:
            # These tools default to dry_run=true on the server. A dry run does
            # not change the host, so it does not need a confirmation prompt.
            if arguments.get("dry_run", True) is True:
                return False
        if self.settings.auto_approve or tool.name in self.always_approved:
            return False
        return True

    async def _execute_tool(self, tool: Tool, arguments: dict[str, Any]) -> tuple[str, bool]:
        started = time.monotonic()
        try:
            async with asyncio.timeout(120):
                result = await self.session.call_tool(tool.name, arguments)
            parts: list[str] = []
            for item in result.content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(text)
            if not parts and result.structuredContent is not None:
                parts.append(json.dumps(result.structuredContent, indent=2, ensure_ascii=False))
            text = "\n".join(parts) or "(empty tool result)"
            text = truncate_bytes(
                text,
                self.settings.max_tool_output_chars,
                "\n...[tool result truncated for the model]",
            )
            if self.session_log:
                self.session_log.record(
                    "tool_result",
                    tool=tool.name,
                    ok=not result.isError,
                    is_error=bool(result.isError),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    result=text,
                )
            return text, bool(result.isError)
        except Exception as exc:
            text = f"Tool execution failed: {type(exc).__name__}: {exc}"
            if self.session_log:
                self.session_log.record(
                    "tool_result",
                    tool=tool.name,
                    ok=False,
                    error=text,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            return text, True

    # ------------------------------------------------------------------- loop
    async def run_turn(self, user_text: str) -> str:
        if not user_text.strip():
            return ""
        self.messages.append({"role": "user", "content": user_text})
        self.last_turns = []
        if self.session_log:
            self.session_log.record("user_message", content=user_text)

        for step in range(1, self.settings.max_steps + 1):
            tools_payload = [self._schema(tool) for tool in self.tools]
            turn = await self.llm.complete(self.messages, tools_payload)

            if turn.content.strip():
                self.printer(f"\nagent> {turn.content.strip()}")
                if self.session_log:
                    self.session_log.record("assistant_message", content=turn.content)

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": turn.content or "",
            }
            if turn.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in turn.tool_calls
                ]
            self.messages.append(assistant_message)

            if not turn.tool_calls:
                return turn.content.strip()

            for call in turn.tool_calls:
                tool = self.tool_map.get(call.name)
                if tool is None:
                    output = (
                        f"Unknown tool {call.name!r}. Available tools: "
                        f"{', '.join(sorted(self.tool_map))}."
                    )
                    is_error = True
                else:
                    try:
                        arguments = json.loads(call.arguments or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        output = f"Invalid JSON arguments for {call.name}: {exc}"
                        is_error = True
                    else:
                        approved = True
                        approval_reason = ""
                        if self.needs_approval(tool, arguments):
                            if self.approval is None:
                                approved = False
                                approval_reason = "no approval callback is configured"
                            else:
                                decision = await self.approval(tool, arguments)
                                approved = decision.allowed
                                approval_reason = decision.reason
                                if decision.allowed and decision.always:
                                    self.always_approved.add(tool.name)
                            if self.session_log:
                                self.session_log.record(
                                    "approval",
                                    tool=tool.name,
                                    args=arguments,
                                    allowed=approved,
                                    reason=approval_reason,
                                )
                        if not approved:
                            output = (
                                f"DENIED: the user did not approve {call.name}. "
                                "Do not retry unless the user asks."
                            )
                            is_error = True
                            self.printer(f"  [denied] {call.name}: {approval_reason}")
                        else:
                            self.printer(f"\n  -> {call.name}({json.dumps(arguments, ensure_ascii=False)})")
                            if self.session_log:
                                self.session_log.record("tool_call", tool=tool.name, args=arguments)
                            output, is_error = await self._execute_tool(tool, arguments)
                            if self.settings.show_tool_output:
                                preview = truncate_bytes(output, 1800)
                                self.printer("  " + preview.replace("\n", "\n  "))

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": output,
                    }
                )
                self.last_turns.append(
                    {
                        "step": step,
                        "tool": call.name,
                        "output": output,
                        "is_error": is_error,
                    }
                )

        message = (
            f"Stopped after {self.settings.max_steps} steps without a final answer. "
            "The last tool results are above; ask me to continue if needed."
        )
        self.printer(f"\nagent> {message}")
        return message


async def open_stdio_session(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
):
    """Open a stdio MCP session. Returns (stack, session)."""
    from contextlib import AsyncExitStack

    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    stack = AsyncExitStack()
    params = StdioServerParameters(command=command, args=args, cwd=cwd, env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return stack, session


async def open_http_session(url: str):
    """Open a Streamable HTTP MCP session. Returns (stack, session)."""
    from contextlib import AsyncExitStack

    from mcp.client.streamable_http import streamablehttp_client

    stack = AsyncExitStack()
    read, write, _ = await stack.enter_async_context(streamablehttp_client(url))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return stack, session
