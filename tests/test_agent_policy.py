from __future__ import annotations

import unittest

from mcp.types import Tool, ToolAnnotations

from linux_agent.agent.client import AgentSettings, MCPAgent


def make_tool(name: str, read_only: bool | None) -> Tool:
    annotations = None if read_only is None else ToolAnnotations(readOnlyHint=read_only)
    return Tool(name=name, description=f"{name} tool", inputSchema={"type": "object"}, annotations=annotations)


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MCPAgent(None, None, AgentSettings())  # type: ignore[arg-type]

    def test_read_only_tool_never_needs_approval(self) -> None:
        tool = make_tool("service_status", True)
        self.assertFalse(self.agent.is_mutating(tool))
        self.assertFalse(self.agent.needs_approval(tool, {}))

    def test_write_tool_dry_run_needs_no_approval(self) -> None:
        tool = make_tool("nginx_set_parameter", False)
        self.assertTrue(self.agent.is_mutating(tool))
        self.assertFalse(self.agent.needs_approval(tool, {"dry_run": True}))
        self.assertFalse(self.agent.needs_approval(tool, {}))  # server default is true
        self.assertTrue(self.agent.needs_approval(tool, {"dry_run": False}))

    def test_unknown_mutating_tool_requires_approval(self) -> None:
        tool = make_tool("delete_everything", None)
        self.assertTrue(self.agent.needs_approval(tool, {}))

    def test_auto_approve(self) -> None:
        self.agent.settings.auto_approve = True
        tool = make_tool("service_action", False)
        self.assertFalse(self.agent.needs_approval(tool, {"dry_run": False}))


if __name__ == "__main__":
    unittest.main()
