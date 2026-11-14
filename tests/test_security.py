from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from linux_agent.config import load_config
from linux_agent.errors import ValidationError
from linux_agent.security import (
    redact_secrets,
    safe_path,
    truncate_bytes,
    unit_base,
    validate_action,
    validate_unit,
)


class UnitValidationTests(unittest.TestCase):
    def test_accepts_common_units(self) -> None:
        self.assertEqual(validate_unit("nginx"), "nginx")
        self.assertEqual(validate_unit("nginx.service"), "nginx.service")
        self.assertEqual(validate_unit("getty@tty1.service"), "getty@tty1.service")
        self.assertEqual(unit_base("nginx.service"), "nginx")
        self.assertEqual(unit_base("nginx"), "nginx")

    def test_rejects_injection(self) -> None:
        for value in ["", "-nginx", "../nginx", "nginx;rm -rf /", "nginx --now", "a b"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_unit(value)

    def test_action_validation(self) -> None:
        self.assertEqual(validate_action("RESTART"), "restart")
        with self.assertRaises(ValidationError):
            validate_action("mask")
        with self.assertRaises(ValidationError):
            validate_action("restart --now")


class RedactionTests(unittest.TestCase):
    def test_redacts_assignments_and_keys(self) -> None:
        text = (
            "db_password=hunter2\n"
            "api_key: sk-abc123\n"
            "Authorization: Bearer abc.def.ghi\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        result = redact_secrets(text)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("sk-abc123", result)
        self.assertNotIn("abc.def.ghi", result)
        self.assertNotIn("secret", result)
        self.assertIn("<redacted>", result)

    def test_truncate_is_byte_bounded(self) -> None:
        text = "é" * 1000
        result = truncate_bytes(text, 100)
        self.assertLessEqual(len(result.encode()), 100 + len("...[output truncated]"))
        self.assertTrue(result.endswith("truncated]"))


class PathTests(unittest.TestCase):
    def test_confines_to_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            root.mkdir()
            good = root / "app.toml"
            good.write_text("x=1", encoding="utf-8")
            self.assertEqual(safe_path("config/app.toml", [root], base=Path(tmp)), good.resolve())
            with self.assertRaises(ValidationError):
                safe_path("/etc/passwd", [root], base=Path(tmp))


class ConfigTests(unittest.TestCase):
    def test_default_policy(self) -> None:
        cfg = load_config()
        self.assertEqual(cfg.server_name, "linux-agent")
        self.assertFalse(cfg.allow_write)
        self.assertIn("nginx", cfg.allowed_units)
        self.assertIn("restart", cfg.allowed_actions)
        self.assertIn("client_max_body_size", cfg.nginx_parameters)

    def test_env_can_enable_write(self) -> None:
        with mock.patch.dict(os.environ, {"LINUX_AGENT_ALLOW_WRITE": "1"}):
            cfg = load_config()
        self.assertTrue(cfg.allow_write)

    def test_parameter_validation(self) -> None:
        cfg = load_config()
        spec = cfg.nginx_parameter("client_max_body_size")
        self.assertEqual(spec.validate("20m"), "20m")
        self.assertEqual(spec.validate("0"), "0")
        for bad in ["20x", "-1m", "99999g", "abc", ""]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                spec.validate(bad)

        workers = cfg.nginx_parameter("worker_processes")
        self.assertEqual(workers.validate("auto"), "auto")
        self.assertEqual(workers.validate("4"), "4")
        for bad in ["0", "65", "auto;", "2.5"]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                workers.validate(bad)

    def test_unknown_parameter_rejected(self) -> None:
        cfg = load_config()
        with self.assertRaises(ValidationError):
            cfg.nginx_parameter("load_module")


if __name__ == "__main__":
    unittest.main()
