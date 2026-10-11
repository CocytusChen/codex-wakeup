"""Event waiting distinguishes process/turn termination from scientific success."""

import unittest
from unittest.mock import patch

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
        import json
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


if __name__ == "__main__":
    unittest.main()
