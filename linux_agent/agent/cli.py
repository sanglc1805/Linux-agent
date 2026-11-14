"""Interactive and one-shot CLI for Linux Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from mcp.types import Tool

from ..audit import AuditLog
from ..config import ROOT, load_config
from ..security import truncate_bytes
from .client import (
    AgentSettings,
    GroqLLM,
    LLMError,
    MCPAgent,
    ToolApproval,
    open_http_session,
    open_stdio_session,
)

load_dotenv(ROOT / ".env")


def _describe_tools(tools: list[Tool]) -> str:
    lines = []
    for tool in sorted(tools, key=lambda item: item.name):
        annotations = tool.annotations
        read_only = bool(annotations and annotations.readOnlyHint)
        marker = "read " if read_only else "write"
        lines.append(f"  [{marker}] {tool.name:<24} {tool.description or ''}")
    return "\n".join(lines)


async def _approval_prompt(tool: Tool, arguments: dict) -> ToolApproval:
    if not sys.stdin.isatty():
        return ToolApproval(
            False,
            "non-interactive stdin; re-run with --auto-approve only for a supervised demo",
        )
    print("\n[!] Approval required for a state-changing tool")
    print(f"    tool: {tool.name}")
    print(f"    what: {tool.description or ''}")
    print(f"    args: {json.dumps(arguments, ensure_ascii=False)}")
    answer = (await asyncio.to_thread(input, "    Approve? [y]es / [N]o / [a]lways: ")).strip().lower()
    if answer in {"y", "yes"}:
        return ToolApproval(True, "approved once")
    if answer in {"a", "always"}:
        return ToolApproval(True, "approved for this session", always=True)
    return ToolApproval(False, "denied by user")


def _print_audit_tail(path: Path, count: int = 12) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError as exc:
        print(f"Cannot read audit log {path}: {exc}")
        return
    print(f"\nLast {len(lines)} audit events ({path}):")
    for line in lines:
        print(truncate_bytes(line, 500))


async def _run(args: argparse.Namespace) -> int:
    cfg = load_config(args.policy)
    settings = cfg.agent_settings
    agent_settings = AgentSettings(
        model=args.model or settings["model"],
        max_steps=args.max_steps or int(settings["max_steps"]),
        max_tool_output_chars=int(settings["max_tool_output_chars"]),
        temperature=float(settings.get("temperature", 0.1)),
        auto_approve=args.auto_approve,
        show_tool_output=not args.quiet,
        system_prompt=(
            "You are Linux Agent, a careful Linux system administrator assistant. "
            "Use the MCP tools to inspect the host before making claims. "
            "For any configuration change: inspect first, use dry_run=true, explain the diff, "
            "and only apply after explicit approval. Never invent command output. "
            "Answer concisely in Markdown.\n\n"
            f"Host: {os.uname().nodename}. "
            f"Write tools enabled: {'yes' if cfg.allow_write else 'no'}. "
            f"Allow-listed service units: {', '.join(sorted(cfg.allowed_units)) or '(none)'}. "
            f"Managed nginx parameters: {', '.join(sorted(cfg.nginx_parameters))}."
        ),
    )

    session_log = AuditLog(
        ROOT
        / "logs"
        / "sessions"
        / f"agent-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.jsonl"
    )
    session_log.record(
        "session_start",
        transport=args.transport,
        url=args.url if args.transport != "stdio" else None,
        model=agent_settings.model,
        write_enabled=cfg.allow_write,
        auto_approve=args.auto_approve,
    )

    if args.transport == "stdio":
        env = dict(os.environ)
        env["LINUX_AGENT_POLICY"] = str(cfg.path)
        stack, session = await open_stdio_session(
            sys.executable,
            [str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=env,
        )
    else:
        stack, session = await open_http_session(args.url)

    async with stack:
        agent = MCPAgent(
            session,
            llm=None,  # type: ignore[arg-type]  # set below when not listing tools
            settings=agent_settings,
            approval=_approval_prompt,
            session_log=session_log,
        )
        tools = await agent.load_tools()

        if args.list_tools:
            print(f"Connected. {len(tools)} tools available:\n")
            print(_describe_tools(tools))
            print(f"\nAudit log: {cfg.audit_log}")
            print(f"Session log: {session_log.path}")
            return 0

        try:
            agent.llm = GroqLLM(
                agent_settings.model,
                agent_settings.temperature,
            )
        except LLMError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2

        print("Linux Agent 1.0 - MCP-based Linux automation")
        print(f"Transport: {args.transport}" + (f" ({args.url})" if args.transport != "stdio" else ""))
        print(f"Model: {agent_settings.model}")
        print(f"Write tools: {'ENABLED' if cfg.allow_write else 'disabled (dry runs still work)'}")
        print(f"Audit: {cfg.audit_log}")
        print(f"Session: {session_log.path}")
        print(f"Tools: {len(tools)}")
        print("Type /help for commands, /quit to exit.\n")
        print(_describe_tools(tools))

        if args.prompt:
            exit_code = 0
            for prompt in args.prompt:
                print(f"\nyou> {prompt}")
                try:
                    await agent.run_turn(prompt)
                except LLMError as exc:
                    print(f"\nagent error: {exc}")
                    exit_code = 1
                    break
            return exit_code

        while True:
            try:
                line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in {"/quit", "/exit", "quit", "exit"}:
                break
            if line == "/help":
                print(
                    "Commands: /tools, /reset, /audit, /policy, /quit\n"
                    "Ask a question in natural language, for example:\n"
                    "  - Is nginx healthy? Show the last 20 log lines.\n"
                    "  - Change client_max_body_size to 20m; show the diff first.\n"
                    "  - What is using the most disk and memory?"
                )
                continue
            if line == "/tools":
                print(_describe_tools(tools))
                continue
            if line == "/reset":
                agent.reset()
                print("Conversation context cleared.")
                continue
            if line == "/audit":
                _print_audit_tail(cfg.audit_log)
                continue
            if line == "/policy":
                print(
                    f"Write: {'on' if cfg.allow_write else 'off'}\n"
                    f"Units: {', '.join(sorted(cfg.allowed_units)) or '(none)'}\n"
                    f"Actions: {', '.join(sorted(cfg.allowed_actions)) or '(none)'}\n"
                    f"Parameters: {', '.join(sorted(cfg.nginx_parameters))}\n"
                    f"Helper (nginx): {cfg.helper('nginx')}\n"
                    f"Helper (service): {cfg.helper('service')}"
                )
                continue
            if line.startswith("/"):
                print("Unknown command. Try /help.")
                continue

            try:
                await agent.run_turn(line)
            except LLMError as exc:
                print(f"\nagent error: {exc}")
            except Exception as exc:  # keep the interactive loop alive
                print(f"\nunexpected error: {type(exc).__name__}: {exc}")

    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="linux-agent",
        description="Agentic Linux administration over MCP.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio spawns the server locally; streamable-http connects to --url.",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/mcp",
        help="Streamable HTTP endpoint (default: %(default)s).",
    )
    parser.add_argument("--policy", default=None, help="Path to policy.toml.")
    parser.add_argument("--model", default=None, help="Groq model id.")
    parser.add_argument("--max-steps", type=int, default=None, help="Maximum agent steps per turn.")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Do not ask before state-changing tools. Use only in a supervised demo.",
    )
    parser.add_argument("--quiet", action="store_true", help="Hide tool result previews.")
    parser.add_argument(
        "--prompt",
        action="append",
        help="Run one prompt and exit (repeatable). Useful for demos and CI.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Connect, list tools and exit without calling the LLM.",
    )
    parser.add_argument("--version", action="version", version="linux-agent 1.0.0")
    args = parser.parse_args(argv)

    if args.max_steps is not None and args.max_steps < 1:
        parser.error("--max-steps must be >= 1")

    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
