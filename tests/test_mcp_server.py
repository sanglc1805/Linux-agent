from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from linux_agent.agent.client import open_stdio_session

ROOT = Path(__file__).resolve().parents[1]


def text_of(result) -> str:
    parts = [getattr(item, "text", "") for item in result.content]
    return "\n".join(part for part in parts if part)


class MCPServerIntegrationTests(unittest.TestCase):
    def test_tools_resources_and_prompts(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ)
                env.update(
                    {
                        "LINUX_AGENT_AUDIT_LOG": str(Path(tmp) / "audit.jsonl"),
                        "LINUX_AGENT_ALLOW_WRITE": "0",
                        "LINUX_AGENT_POLICY": str(ROOT / "config" / "policy.toml"),
                    }
                )
                stack, session = await open_stdio_session(
                    sys.executable,
                    [str(ROOT / "server.py")],
                    cwd=str(ROOT),
                    env=env,
                )
                try:
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertGreaterEqual(len(names), 20)
                    self.assertIn("system_overview", names)
                    self.assertIn("nginx_set_parameter", names)
                    self.assertIn("service_action", names)
                    self.assertIn("read_config", names)

                    by_name = {tool.name: tool for tool in tools.tools}
                    self.assertTrue(by_name["system_overview"].annotations.readOnlyHint)
                    self.assertFalse(by_name["nginx_set_parameter"].annotations.readOnlyHint)

                    overview = await session.call_tool("system_overview", {})
                    self.assertFalse(overview.isError, text_of(overview))
                    self.assertIn("Host:", text_of(overview))

                    disk = await session.call_tool("disk_usage", {})
                    self.assertFalse(disk.isError, text_of(disk))
                    self.assertIn("Filesystem", text_of(disk))

                    config = await session.call_tool(
                        "read_config", {"path": "config/policy.toml", "max_lines": 10}
                    )
                    self.assertFalse(config.isError, text_of(config))
                    self.assertIn("policy", text_of(config))

                    nginx = await session.call_tool("nginx_list_parameters", {})
                    self.assertFalse(nginx.isError, text_of(nginx))
                    self.assertIn("worker_processes", text_of(nginx))

                    dry = await session.call_tool(
                        "nginx_set_parameter",
                        {
                            "parameter": "client_max_body_size",
                            "value": "20m",
                            "dry_run": True,
                        },
                    )
                    self.assertFalse(dry.isError, text_of(dry))
                    self.assertIn("DRY RUN", text_of(dry))
                    self.assertIn("+client_max_body_size 20m;", text_of(dry))

                    # Writes are disabled in this test process.
                    blocked = await session.call_tool(
                        "nginx_set_parameter",
                        {
                            "parameter": "client_max_body_size",
                            "value": "20m",
                            "dry_run": False,
                        },
                    )
                    self.assertTrue(blocked.isError)
                    self.assertIn("disabled", text_of(blocked))

                    preview = await session.call_tool(
                        "service_action",
                        {"unit": "nginx", "action": "restart", "dry_run": True},
                    )
                    self.assertFalse(preview.isError, text_of(preview))
                    self.assertIn("DRY RUN", text_of(preview))

                    disallowed = await session.call_tool(
                        "service_action",
                        {"unit": "ssh", "action": "restart", "dry_run": True},
                    )
                    self.assertTrue(disallowed.isError)
                    self.assertIn("not allow-listed", text_of(disallowed))

                    resources = await session.list_resources()
                    uris = {str(resource.uri) for resource in resources.resources}
                    self.assertIn("linux://policy", uris)
                    self.assertIn("linux://system/summary", uris)

                    policy = await session.read_resource("linux://policy")
                    policy_text = policy.contents[0].text
                    self.assertIn("write_enabled", policy_text)
                    self.assertIn("client_max_body_size", policy_text)

                    summary = await session.read_resource("linux://system/summary")
                    self.assertIn("Host:", summary.contents[0].text)

                    prompts = await session.list_prompts()
                    prompt_names = {prompt.name for prompt in prompts.prompts}
                    self.assertIn("triage_service", prompt_names)
                    self.assertIn("change_nginx_parameter", prompt_names)

                    prompt = await session.get_prompt(
                        "triage_service", {"unit": "nginx"}
                    )
                    self.assertIn("nginx", prompt.messages[0].content.text)
                finally:
                    await stack.aclose()

        asyncio.run(scenario())

    def test_write_path_with_unprivileged_fake_helper(self) -> None:
        """Exercise apply -> verify -> rollback without touching the real host."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                conf = tmp_path / "nginx.conf"
                conf.write_text(
                    "worker_processes auto;\n"
                    "events { worker_connections 768; }\n",
                    encoding="utf-8",
                )
                dropin_dir = tmp_path / "conf.d"
                dropin_dir.mkdir()
                dropin = dropin_dir / "99-linux-agent.conf"
                fake_nginx = tmp_path / "fake-nginx"
                fake_nginx.write_text(
                    "#!/bin/sh\nexit 0\n",
                    encoding="utf-8",
                )
                fake_nginx.chmod(0o755)
                fake_systemctl = tmp_path / "systemctl"
                fake_systemctl.write_text(
                    "#!/bin/sh\nif [ \"$1\" = \"is-active\" ]; then echo active; fi\nexit 0\n",
                    encoding="utf-8",
                )
                fake_systemctl.chmod(0o755)

                # The server deliberately does not forward its environment to a
                # privileged helper. This wrapper injects the fake paths for the
                # test only, while still exercising the real helper script.
                wrapper = tmp_path / "nginx-helper-wrapper"
                wrapper.write_text(
                    "#!/bin/sh\n"
                    f'export NGINX_CONF="{conf}"\n'
                    f'export NGINX_CONF_DIR="{tmp_path}"\n'
                    f'export NGINX_DROPIN_DIR="{dropin_dir}"\n'
                    f'export NGINX_DROPIN="{dropin}"\n'
                    f'export NGINX_BIN="{fake_nginx}"\n'
                    f'export BACKUP_DIR="{tmp_path / "backups"}"\n'
                    f'export AUDIT_LOG="{tmp_path / "helper-audit.log"}"\n'
                    f'export SYSTEMCTL_BIN="{fake_systemctl}"\n'
                    f'export TMPDIR="{tmp_path}"\n'
                    f'exec "{ROOT / "scripts" / "nginx_param.sh"}" "$@"\n',
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)

                env = dict(os.environ)
                env.update(
                    {
                        "LINUX_AGENT_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
                        "LINUX_AGENT_ALLOW_WRITE": "1",
                        "LINUX_AGENT_USE_SUDO": "0",
                        "LINUX_AGENT_NGINX_HELPER": str(wrapper),
                        "LINUX_AGENT_POLICY": str(ROOT / "config" / "policy.toml"),
                    }
                )
                stack, session = await open_stdio_session(
                    sys.executable,
                    [str(ROOT / "server.py")],
                    cwd=str(ROOT),
                    env=env,
                )
                try:
                    applied = await session.call_tool(
                        "nginx_set_parameter",
                        {
                            "parameter": "client_max_body_size",
                            "value": "20m",
                            "dry_run": False,
                            "reload": False,
                        },
                    )
                    self.assertFalse(applied.isError, text_of(applied))
                    self.assertIn("APPLIED", text_of(applied))
                    self.assertIn("client_max_body_size 20m;", dropin.read_text())

                    current = await session.call_tool(
                        "nginx_get_parameter", {"parameter": "client_max_body_size"}
                    )
                    self.assertFalse(current.isError, text_of(current))
                    self.assertIn("20m", text_of(current))

                    backups = await session.call_tool("nginx_list_backups", {})
                    self.assertFalse(backups.isError, text_of(backups))
                    self.assertIn("new=20m", text_of(backups))

                    audit_text = (tmp_path / "audit.jsonl").read_text()
                    self.assertIn('"tool": "nginx_set_parameter"', audit_text)
                    self.assertIn('"ok": true', audit_text)
                finally:
                    await stack.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
