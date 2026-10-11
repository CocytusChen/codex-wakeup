import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_wakeup import FakeClient, FakeManager

from codex_wakeup.api import job_summary as public_job
from codex_wakeup.engine import JobStore, ToolError, cancel, deliver, is_active_job, wake_message
from codex_wakeup.jobs import execute_trigger, identity, read_output, submit


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "state")
        self.client = FakeClient(str(self.root))
        self.manager = FakeManager()

    def command(self, code, **kwargs):
        return self.arrange(
            {
                "type": "command",
                "argv": [sys.executable, "-c", code],
                "cwd": str(self.root),
                **kwargs,
            }
        )

    def arrange(self, trigger, **kwargs):
        return submit(
            self.store,
            lambda _p: self.client,
            thread="test-only",
            socket_path=self.root / "fake",
            instructions="Inspect this contained fixture result.",
            trigger=trigger,
            manager=self.manager,
            **kwargs,
        )

    def execute(self, job):
        self.assertTrue(execute_trigger(self.store, job["id"]))
        return self.store.read(job["id"])

    def test_invalid_target_cannot_execute_command(self):
        self.client.exists = False
        with self.assertRaises(ToolError):
            self.command("open('effect', 'w').write('bad')")
        self.assertFalse((self.root / "effect").exists())
        self.assertFalse(self.store.root.exists())

    def test_dry_run_and_invalid_command_start_nothing(self):
        self.arrange(
            {
                "type": "command",
                "argv": [sys.executable, "-c", "raise RuntimeError()"],
                "cwd": str(self.root),
            },
            dry_run=True,
        )
        self.assertFalse(self.store.root.exists())
        for argv in [[], ["missing-executable-4a2ca6"], [sys.executable, "bad\0value"]]:
            with self.assertRaises(ToolError):
                self.arrange({"type": "command", "argv": argv, "cwd": str(self.root)})

    def test_nonzero_exit_is_result_not_registration_failure(self):
        job = self.command("print('retained result'); raise SystemExit(7)")
        result = self.execute(job)
        self.assertEqual(result["execution"]["status"], "exited")
        self.assertEqual(result["execution"]["exit_code"], 7)
        self.assertIn("retained result", read_output(self.store, job["id"])["text"])
        self.assertEqual(result["delivery"]["status"], "scheduled")
        delivered, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(delivered["delivery"]["status"], "accepted")

    def test_small_output_returned_at_wakeup_without_file(self):
        job = self.command("print('hello')", output_mode="message", output_limit=6)
        result = self.execute(job)
        self.assertEqual(result["execution"]["output_text"], "hello\n")
        self.assertIsNone(result["execution"]["output_path"])
        self.assertIn("hello\n", wake_message(result, self.store))
        self.assertNotIn("output_text", public_job(result)["execution"])

    def test_default_one_kib_boundary_measures_bytes_and_keeps_complete_output(self):
        for size in (1024, 1025):
            with self.subTest(size=size):
                job = self.command(f"import os; os.write(1, b'x'*{size})", output_mode="message")
                self.assertEqual(job["trigger"]["output_limit"], 1024)
                execution = self.execute(job)["execution"]
                if size == 1024:
                    self.assertEqual(execution["output_text"], "x" * size)
                    self.assertIsNone(execution["output_path"])
                else:
                    self.assertNotIn("output_text", execution)
                    self.assertEqual(Path(execution["output_path"]).read_bytes(), b"x" * size)
        job = self.command("import sys; sys.stdout.write('界'*342)", output_mode="message")
        execution = self.execute(job)["execution"]
        self.assertEqual(execution["output_bytes_seen"], 1026)
        self.assertNotIn("output_text", execution)
        self.assertEqual(Path(execution["output_path"]).read_text(), "界" * 342)

    def test_active_lifecycle_tracks_execution_and_submission(self):
        job = self.command("pass")
        self.assertTrue(is_active_job(job))
        job = self.execute(job)
        self.assertTrue(is_active_job(job))
        for state in ("deferred", "retry_wait", "dispatching"):
            job["delivery"]["status"] = state
            self.assertTrue(is_active_job(job))
        for state in ("accepted", "uncertain", "rejected", "failed", "cancelled"):
            job["delivery"]["status"] = state
            self.assertFalse(is_active_job(job))
        self.assertFalse(is_active_job(job))
        job["execution"]["status"] = "cancel_requested"
        self.assertTrue(is_active_job(job))

    def test_cross_conversation_cancel_has_no_effect_even_with_known_id(self):
        job = self.command("open('effect','w').write('bad')")
        before = self.store.path(job["id"]).read_bytes()
        with self.assertRaises(ToolError) as caught:
            cancel(self.store, self.manager, job["id"], thread="another-thread")
        self.assertEqual(caught.exception.code, "job_not_found")
        self.assertEqual(self.store.path(job["id"]).read_bytes(), before)
        self.assertFalse((self.root / "effect").exists())

    def test_overflow_preserves_entire_output_and_sends_path_only(self):
        job = self.command(
            "print('START'+'x'*200000+'END')", output_mode="message", output_limit=32
        )
        result = self.execute(job)
        execution = result["execution"]
        self.assertNotIn("output_text", execution)
        self.assertTrue(execution["output_spilled"])
        self.assertFalse(execution["output_truncated"])
        full = Path(execution["output_path"]).read_text()
        self.assertEqual(full, "START" + "x" * 200000 + "END\n")
        message = wake_message(result, self.store)
        self.assertIn(execution["output_path"], message)
        self.assertNotIn("STARTxxxx", message)

    def test_file_mode_preserves_small_output_at_declared_path(self):
        path = self.root / "explicit-output.txt"
        job = self.command("print('hello')", output_mode="file", output_file=str(path))
        result = self.execute(job)["execution"]
        self.assertEqual(path.read_text(), "hello\n")
        self.assertEqual(result["output_path"], str(path))
        self.assertNotIn("output_text", result)

    def test_existing_or_nonabsolute_output_file_refuses_before_command(self):
        path = self.root / "important.txt"
        path.write_text("original")
        for filename in [str(path), "relative.txt", str(self.root / "absent" / "out")]:
            with self.assertRaises(ToolError):
                self.command(
                    "open('effect','w').write('bad')", output_mode="file", output_file=filename
                )
        self.assertEqual(path.read_text(), "original")
        self.assertFalse((self.root / "effect").exists())

    def test_invalid_utf8_expansion_spills_rather_than_exceeding_message_limit(self):
        job = self.command(
            "import os; os.write(1, b'\\xff'*8)", output_mode="message", output_limit=8
        )
        result = self.execute(job)["execution"]
        self.assertNotIn("output_text", result)
        self.assertEqual(Path(result["output_path"]).read_bytes(), b"\xff" * 8)

    def test_argv_is_not_shell_interpreted_or_echoed_in_status(self):
        literal = "`touch bad` $(touch worse)"
        job = self.arrange(
            {
                "type": "command",
                "cwd": str(self.root),
                "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", literal],
            }
        )
        self.execute(job)
        self.assertEqual(read_output(self.store, job["id"])["text"].strip(), literal)
        self.assertFalse((self.root / "bad").exists())
        self.assertFalse((self.root / "worse").exists())
        self.assertNotIn("trigger", public_job(job))
        self.assertNotIn(literal, json.dumps(public_job(job)))

    def test_optional_timeout_stops_owned_child(self):
        job = self.command("import time; time.sleep(30)", timeout_seconds=0.1)
        result = self.execute(job)["execution"]
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["owned_group_settlement"])
        self.assertIsNone(identity(result["process"]["pid"]))

    def test_restarting_worker_cannot_repeat_committed_effect(self):
        job = self.command("open('effect', 'a').write('once')")
        self.execute(job)
        self.execute(job)
        self.assertEqual((self.root / "effect").read_text(), "once")
        updated = self.store.read(job["id"])
        updated["execution"] = {
            "status": "starting",
            "runner": {"pid": 99999999, "start_ticks": "0"},
        }
        self.store.save(updated)
        result = self.execute(updated)
        self.assertEqual(result["execution"]["status"], "unknown")
        self.assertEqual((self.root / "effect").read_text(), "once")

    def test_second_worker_does_not_wake_while_original_running(self):
        job = self.command("raise RuntimeError('must not execute')")
        job["execution"] = {
            "status": "running",
            "runner": {"pid": os.getpid(), "start_ticks": identity(os.getpid())["start_ticks"]},
        }
        self.store.save(job)
        self.assertFalse(execute_trigger(self.store, job["id"]))
        self.assertFalse(self.client.sent)
        self.assertEqual(self.store.read(job["id"])["execution"]["status"], "running")

    def test_preexecution_cancel_has_no_effect(self):
        job = self.command("open('effect', 'w').write('bad')")
        job["delivery"]["status"] = "cancelled"
        self.store.save(job)
        self.assertEqual(self.execute(job)["execution"]["status"], "cancelled")
        self.assertFalse((self.root / "effect").exists())

    def test_manual_delivery_cannot_wake_before_execution(self):
        job = self.command("pass")
        _result, code = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(code, 75)
        self.assertFalse(self.client.sent)

    def test_actual_cancel_before_claim_is_final_and_does_not_run(self):
        job = self.command("open('effect', 'w').write('bad')")
        cancelled, _ = cancel(self.store, self.manager, job["id"])
        self.assertEqual(cancelled["execution"]["status"], "cancelled")
        self.assertEqual(self.execute(job)["execution"]["status"], "cancelled")
        self.assertFalse((self.root / "effect").exists())

    def test_missing_or_zombie_process_refuses_registration(self):
        with self.assertRaises(ToolError):
            self.arrange({"type": "process_exit", "pid": 99999999})
        with patch("codex_wakeup.jobs.identity", return_value={"pid": 42, "state": "Z"}):
            with self.assertRaises(ToolError):
                self.arrange({"type": "process_exit", "pid": 42})
        self.assertFalse(self.store.root.exists())

    def test_watch_detects_pid_reuse_without_signalling_replacement(self):
        current = identity(os.getpid())
        job = self.arrange({"type": "process_exit", "pid": os.getpid()})
        replacement = dict(current, start_ticks="different")
        with patch("codex_wakeup.jobs.identity", return_value=replacement):
            with patch("codex_wakeup.jobs.os.killpg") as kill:
                result = self.execute(job)
                kill.assert_not_called()
        self.assertEqual(result["execution"]["status"], "exited")
        self.assertIsNone(result["execution"]["exit_code"])

    def test_watch_timeout_never_kills_watched_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            job = self.arrange({"type": "process_exit", "pid": child.pid, "timeout_seconds": 0.05})
            result = self.execute(job)
            self.assertEqual(result["execution"]["status"], "timed_out")
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
