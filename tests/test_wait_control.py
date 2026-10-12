"""Event waiting distinguishes process/turn termination from scientific success."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_detached_jobs import FakeDaemon

from codex_wakeup import waiting as wait_control


class WaitControlTest(unittest.TestCase):
    def test_healthy_turn_and_process_do_not_wake(self):
        value = {
            "status": "inProgress",
            "error_present": False,
            "process": {"pid": 1, "start_ticks": "2", "boot_id": "a", "state": "S"},
        }
        self.assertIsNone(wait_control.event(value, value))

    def test_process_reuse_and_disappearance_wake_without_success_claim(self):
        value = {
            "status": "inProgress",
            "error_present": False,
            "process": {"pid": 1, "start_ticks": "2", "boot_id": "a", "state": "S"},
        }
        for process in [
            None,
            dict(value["process"], start_ticks="3"),
            dict(value["process"], state="Z"),
        ]:
            self.assertEqual(
                wait_control.event(value, dict(value, process=process)), "original_process_ended"
            )

    def test_completed_failed_and_interrupted_turns_are_events(self):
        for status in ["completed", "failed", "interrupted"]:
            self.assertEqual(
                wait_control.event({}, {"status": status, "error_present": False}),
                "owner_turn_terminal",
            )

    def test_remote_probe_uses_installed_command_without_source_transfer(self):
        from argparse import Namespace
        from subprocess import CompletedProcess

        args = Namespace(
            host="compute-node",
            thread="owner",
            turn=None,
            pid=123,
            socket=None,
            remote_command="/opt/user tools/codex-wakeup-wait",
            remote_socket="/tmp/control.sock",
        )
        with patch.object(
            wait_control.subprocess,
            "run",
            return_value=CompletedProcess(
                [], 0, json.dumps({"ok": True, "data": {"process": {"pid": 123}}}), ""
            ),
        ) as run:
            self.assertEqual(wait_control.snapshot(args)["process"]["pid"], 123)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:4], ["ssh", "-oBatchMode=yes", "-oConnectTimeout=12", "compute-node"]
        )
        self.assertIn("'/opt/user tools/codex-wakeup-wait'", command[-1])
        self.assertNotIn("-c ", command[-1])

    def test_custom_codex_directory_controls_default_socket(self):
        from codex_wakeup.config import control_socket_path

        with patch.dict("os.environ", {"CODEX_HOME": "/tmp/custom-codex"}, clear=True):
            self.assertEqual(
                str(control_socket_path()),
                "/tmp/custom-codex/app-server-control/app-server-control.sock",
            )


class WaitHelperIntegrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.daemon = FakeDaemon(self.root)
        self.addCleanup(self.daemon.close)
        self.entry = Path(__file__).resolve().parents[1] / "wait_control.py"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.helper = self.bin / "codex-wakeup-wait"
        self.helper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(self.entry)!r}]"
            " + sys.argv[1:])\n"
        )
        self.helper.chmod(0o700)
        self.probes = self.root / "probes.jsonl"
        ssh = self.bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\nimport json, os, shlex, subprocess, sys\n"
            "result = subprocess.run(shlex.split(sys.argv[-1]), capture_output=True, text=True)\n"
            "with open(os.environ['WAKE_TEST_PROBES'], 'a') as log:\n"
            "    log.write(json.dumps({'returncode': result.returncode,"
            " 'stdout': result.stdout}) + '\\n')\n"
            "sys.stdout.write(result.stdout)\nsys.stderr.write(result.stderr)\n"
            "raise SystemExit(result.returncode)\n"
        )
        ssh.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "WAKE_TEST_PROBES": str(self.probes),
        }

    def process(self, argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)

        def close():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

        self.addCleanup(close)
        return process

    def idle_process(self):
        return self.process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def command(self, action, pid, remote=False, socket_path=None):
        command = [sys.executable, str(self.entry), action, "--thread", "fixture-owner"]
        command += ["--pid", str(pid), "--interval", "5"]
        socket_path = str(socket_path or self.daemon.path)
        if remote:
            command += [
                "--host",
                "fixture-node",
                "--remote-command",
                str(self.helper),
                "--remote-socket",
                socket_path,
            ]
        else:
            command += ["--socket", socket_path]
        return command

    def invoke(self, *args, **kwargs):
        result = subprocess.run(
            self.command(*args, **kwargs),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, json.loads(result.stdout)

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Fixture did not reach the expected state.")
            time.sleep(0.01)

    def test_probe_reports_zombie_and_missing_process_without_refusing(self):
        child = self.process([sys.executable, "-c", "pass"])
        stat = Path(f"/proc/{child.pid}/stat")
        self.until(lambda: stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")
        code, value = self.invoke("probe", child.pid, remote=True)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["data"]["process"]["state"], "Z")
        child.wait(timeout=5)
        code, value = self.invoke("probe", child.pid, remote=True)
        self.assertEqual(code, 0, value)
        self.assertIsNone(value["data"]["process"])

    def test_wait_refuses_missing_process_at_local_and_remote_preflight(self):
        child = self.idle_process()
        child.terminate()
        child.wait(timeout=5)
        for remote in (False, True):
            with self.subTest(remote=remote):
                code, value = self.invoke("wait", child.pid, remote=remote)
                self.assertEqual(code, 1, value)
                self.assertFalse(value["ok"])
                self.assertEqual(value["error"], "Monitored process must be running at preflight.")

    def test_remote_wait_classifies_exit_after_real_successful_preflight(self):
        child = self.idle_process()
        observer = self.process(
            self.command("wait", child.pid, remote=True),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.until(lambda: self.probes.exists() and "\n" in self.probes.read_text())
        first = json.loads(self.probes.read_text().splitlines()[0])
        self.assertEqual(first["returncode"], 0, first)
        initial = json.loads(first["stdout"])
        self.assertEqual(initial["data"]["process"]["pid"], child.pid)
        self.assertNotEqual(initial["data"]["process"]["state"], "Z")
        child.terminate()
        child.wait(timeout=5)
        stdout, stderr = observer.communicate(timeout=10)
        value = json.loads(stdout)
        self.assertEqual(observer.returncode, 0, (value, stderr))
        self.assertEqual(value["data"]["event"], "original_process_ended")
        self.assertIsNone(value["data"]["observed"]["process"])
        self.assertEqual(value["data"]["business_success"], "unassessed")
        self.assertGreaterEqual(len(self.probes.read_text().splitlines()), 2)
        self.assertTrue(
            all(method in {"initialize", "thread/read"} for method in self.daemon.calls)
        )

    def test_remote_endpoint_failure_is_not_reported_as_process_exit(self):
        child = self.idle_process()
        code, value = self.invoke(
            "wait", child.pid, remote=True, socket_path=self.root / "missing.sock"
        )
        self.assertEqual(code, 1, value)
        self.assertFalse(value["ok"])
        self.assertEqual(
            value["error"],
            "Remote observation failed; experiment state is unknown, not failed.",
        )
        self.assertNotIn("data", value)
        self.assertIsNone(child.poll())


if __name__ == "__main__":
    unittest.main()
