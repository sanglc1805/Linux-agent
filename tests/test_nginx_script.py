from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nginx_param.sh"


class NginxHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-agent-nginx-test-"))
        self.conf = self.tmp / "nginx.conf"
        self.dropin_dir = self.tmp / "conf.d"
        self.dropin = self.dropin_dir / "99-linux-agent.conf"
        self.backups = self.tmp / "backups"
        self.dropin_dir.mkdir()
        self.conf.write_text(
            "user www-data;\n"
            "worker_processes auto;\n"
            "events {\n"
            "    worker_connections 768;\n"
            "}\n"
            "http {\n"
            "    include /etc/nginx/mime.types;\n"
            "}\n",
            encoding="utf-8",
        )
        self.fake_nginx = self.tmp / "fake-nginx"
        self.fake_nginx.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-v" ]; then echo "nginx version: nginx/1.28.0" >&2; exit 0; fi\n'
            'if [ "$1" = "-t" ]; then\n'
            '  if [ "${FAIL_TEST:-0}" = "1" ]; then echo "nginx: [emerg] bad config" >&2; exit 1; fi\n'
            '  echo "nginx: configuration file test is successful" >&2; exit 0;\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.fake_nginx.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "NGINX_CONF": str(self.conf),
                "NGINX_CONF_DIR": str(self.tmp),
                "NGINX_DROPIN_DIR": str(self.dropin_dir),
                "NGINX_BIN": str(self.fake_nginx),
                "BACKUP_DIR": str(self.backups),
                "AUDIT_LOG": str(self.tmp / "audit.log"),
                "SYSTEMCTL_BIN": "/bin/true",
                "TMPDIR": str(self.tmp),
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(SCRIPT), *args],
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )

    def test_list_and_get(self) -> None:
        result = self.run_helper("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker_processes", result.stdout)
        self.assertIn("auto", result.stdout)

        result = self.run_helper("get", "worker_processes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "auto")

    def test_dry_run_does_not_write(self) -> None:
        result = self.run_helper("set", "client_max_body_size", "20m", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+client_max_body_size 20m;", result.stdout)
        self.assertFalse(self.dropin.exists())

    def test_set_http_parameter_and_backup(self) -> None:
        result = self.run_helper("set", "client_max_body_size", "20m")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dropin.is_file())
        self.assertIn("client_max_body_size 20m;", self.dropin.read_text())
        self.assertEqual(self.run_helper("get", "client_max_body_size").stdout.strip(), "20m")
        backups = [path for path in self.backups.iterdir() if path.is_dir()]
        self.assertEqual(len(backups), 1)
        self.assertIn("new=20m", (backups[0] / "meta").read_text())

    def test_set_main_and_events_parameters(self) -> None:
        result = self.run_helper("set", "worker_processes", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker_processes 4;", self.conf.read_text())

        result = self.run_helper("set", "worker_connections", "4096")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker_connections 4096;", self.conf.read_text())

    def test_invalid_value_is_rejected(self) -> None:
        before = self.conf.read_text()
        result = self.run_helper("set", "client_max_body_size", "20x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("client_max_body_size", result.stderr)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.conf.read_text(), before)

    def test_failed_config_test_removes_new_file(self) -> None:
        result = self.run_helper(
            "set", "gzip", "on", extra_env={"FAIL_TEST": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restored", result.stderr)
        self.assertFalse(self.dropin.exists())

    def test_failed_config_test_restores_existing_file(self) -> None:
        self.dropin.write_text("gzip off;\n", encoding="utf-8")
        result = self.run_helper(
            "set", "gzip", "on", extra_env={"FAIL_TEST": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.dropin.read_text(), "gzip off;\n")

    def test_rollback(self) -> None:
        self.assertEqual(self.run_helper("set", "client_max_body_size", "10m").returncode, 0)
        first_id = sorted(path.name for path in self.backups.iterdir() if path.is_dir())[0]
        self.assertEqual(self.run_helper("set", "client_max_body_size", "20m").returncode, 0)
        self.assertEqual(self.run_helper("get", "client_max_body_size").stdout.strip(), "20m")

        result = self.run_helper("rollback", first_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_helper("get", "client_max_body_size").stdout.strip(), "10m")

    def test_unknown_parameter(self) -> None:
        result = self.run_helper("set", "load_module", "/tmp/evil.so")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown parameter", result.stderr)


class ServiceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-agent-service-test-"))
        self.allow = self.tmp / "allowed-units"
        self.allow.write_text("# comment\nnginx\n", encoding="utf-8")
        self.log = self.tmp / "systemctl.log"
        self.fake_systemctl = self.tmp / "systemctl"
        self.fake_systemctl.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.log}"\n'
            'if [ "$1" = "is-active" ]; then echo active; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        self.fake_systemctl.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "ALLOWED_UNITS_FILE": str(self.allow),
                "SYSTEMCTL_BIN": str(self.fake_systemctl),
                "ACTION_LOG": str(self.tmp / "actions.log"),
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(ROOT / "scripts" / "service_ctl.sh"), *args],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=20,
        )

    def test_allowed_action(self) -> None:
        result = self.run_helper("restart", "nginx")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: restart nginx.service", result.stdout)
        self.assertIn("restart -- nginx.service", self.log.read_text())

    def test_service_suffix_is_normalized(self) -> None:
        result = self.run_helper("reload", "nginx.service")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disallowed_unit(self) -> None:
        result = self.run_helper("stop", "ssh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not in the allow-list", result.stderr)
        self.assertFalse(self.log.exists())

    def test_invalid_action_and_unit(self) -> None:
        self.assertNotEqual(self.run_helper("mask", "nginx").returncode, 0)
        self.assertNotEqual(self.run_helper("start", "-bad").returncode, 0)
        self.assertNotEqual(self.run_helper("start", "../nginx").returncode, 0)


if __name__ == "__main__":
    unittest.main()
