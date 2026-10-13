"""Native queue/steer delivery, timing, cancellation and uncertainty."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codex_wakeup.engine import (
    RETRY,
    JobStore,
    ToolError,
    cancel,
    deliver,
    instruction_text,
    schedule,
    wait_for_job,
)
from codex_wakeup.rpc import RpcError


class FakeClient:
    def __init__(self, cwd):
        self.cwd, self.active, self.loaded, self.exists = cwd, None, True, True
        self.failure, self.race, self.queue_available = None, False, True
        self.start_failure, self.start_race, self.start_response = None, False, None
        self.started_queue_ids = []
        self.cli_version = "0.162.1"
        self.initialize_result = {"userAgent": "Codex/0.162.1 (fixture)"}
        self.saved_permissions = {"approvalPolicy": "never", "permissionProfile": "read-only"}
        self.permissions = dict(self.saved_permissions)
        self.has_completed_turn = True
        self.sent, self.calls = [], []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            if not self.exists:
                raise RpcError({"message": "thread not found"})
            return {
                "thread": {
                    "cwd": self.cwd,
                    "cliVersion": self.cli_version,
                    "status": {
                        "type": "active" if self.active else "idle" if self.loaded else "notLoaded"
                    },
                }
            }
        if method == "thread/turns/list":
            return {
                "data": [{"id": self.active, "status": "inProgress"}]
                if self.active
                else [{"id": "previous-turn", "status": "completed"}]
                if self.has_completed_turn
                else []
            }
        if method == "thread/queue/list":
            if not self.queue_available:
                raise RpcError({"message": "unsupported queue"})
            return {"data": []}
        if method == "thread/queue/start":
            if self.start_race:
                self.active = "racing-turn"
                raise RpcError({"message": "thread already has an active or pending turn"})
            if self.start_failure:
                raise self.start_failure
            self.started_queue_ids.append(params["queuedSubmissionId"])
            return (
                self.start_response
                if self.start_response is not None
                else {"turn": {"id": "new-turn"}}
            )
        if method == "thread/resume":
            if set(params) != {"threadId", "excludeTurns"}:
                raise AssertionError("Resume must have no settings overrides")
            self.loaded = True
            self.permissions = dict(self.saved_permissions)
            return {"thread": {"id": params["threadId"]}, **self.permissions}
        self.sent.append((method, params))
        if self.race:
            self.race = False
            self.active = None
            raise RpcError({"message": "no active turn to steer"})
        if self.failure:
            raise self.failure
        if method == "thread/queue/add":
            return {
                "queuedSubmission": {
                    "id": "native-queue-item",
                    "clientUserMessageId": params["clientUserMessageId"],
                    "input": params["input"],
                }
            }
        if method == "turn/steer":
            return {"turnId": self.active}
        if method == "turn/start":
            return {"turn": {"id": "new-turn"}}
        raise AssertionError(f"Unexpected or settings-mutating method: {method}")


class FakeManager:
    def __init__(self):
        self.installed, self.removed, self.fail_install = [], [], False

    def preflight(self):
        pass

    def verify(self, _job):
        pass

    def install(self, job):
        self.installed.append(job["id"])
        if self.fail_install:
            raise ToolError("worker_start_failed", "Failed registration")

    def remove(self, job):
        self.removed.append(job["id"])


class WakeupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store, self.client, self.manager = (
            JobStore(self.root / "state"),
            FakeClient(tmp.name),
            FakeManager(),
        )

    def arrange(self, **overrides):
        args = {
            "thread": "existing-thread",
            "socket_path": self.root / "control.sock",
            "instructions": "Inspect existing work.",
            "after": "3h",
        }
        args.update(overrides)
        return schedule(self.store, self.manager, lambda _p: self.client, **args)

    def due(self, **kwargs):
        job = self.arrange(**kwargs)
        job["scheduled_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.store.save(job)
        return job

    def test_invalid_target_or_queue_cannot_register(self):
        for attribute in ("exists", "queue_available"):
            with self.subTest(attribute=attribute):
                setattr(self.client, attribute, False)
                with self.assertRaises((ToolError, RpcError)):
                    self.arrange()
                setattr(self.client, attribute, True)
        self.assertFalse(self.manager.installed)
        self.assertFalse(self.store.root.exists())

    def test_invalid_times_and_content_start_nothing(self):
        for kwargs in [
            {"after": None, "at": "2020-01-01T00:00:00Z"},
            {"after": None, "at": "2030-01-01T00:00:00"},
            {"instructions": ""},
            {"instructions": "x" * 65537},
        ]:
            with self.subTest(kwargs=str(kwargs)[:100]), self.assertRaises(ToolError):
                self.arrange(**kwargs)
        self.assertFalse(self.manager.installed)

    def test_dry_run_creates_no_job_or_worker(self):
        self.arrange(dry_run=True)
        self.assertFalse(self.store.root.exists())
        self.assertFalse(self.manager.installed)

    def test_failed_registration_retains_record(self):
        self.manager.fail_install = True
        with self.assertRaises(ToolError):
            self.arrange()
        job = self.store.read(self.manager.installed[0])
        self.assertEqual(job["delivery"]["status"], "registration_failed")
        self.assertEqual(self.manager.removed, self.manager.installed)

    def test_queue_busy_or_unloaded_submits_once_and_worker_stops_without_poll(self):
        for active, loaded in [("busy-turn", True), (None, False)]:
            with self.subTest(active=active):
                self.client.active, self.client.loaded = active, loaded
                self.client.cli_version = "0.162.1" if loaded else "0.148.0"
                job = self.due()
                self.client.calls.clear()
                sleeps = []
                result = wait_for_job(
                    self.store, lambda _p: self.client, job["id"], sleeper=sleeps.append
                )
                self.assertEqual(
                    [m for m, _ in self.client.calls],
                    ["thread/queue/add", "thread/read", "thread/turns/list"],
                )
                self.assertFalse(sleeps)
                self.assertEqual(result["delivery"]["status"], "accepted")
                self.assertEqual(result["delivery"]["queue_id"], "native-queue-item")
                self.assertEqual(self.client.loaded, loaded)
                self.assertFalse(self.client.started_queue_ids)
                if not loaded:
                    self.assertIn("saved permission profile", result["delivery"]["error"])

    def test_unloaded_resumes_saved_settings_without_overrides_then_starts(self):
        job = self.due()
        self.client.loaded = False
        self.client.calls.clear()
        before = dict(self.client.permissions)
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        methods = [m for m, _ in self.client.calls]
        self.assertIn("thread/resume", methods)
        self.assertEqual(methods[-1], "thread/queue/start")
        self.assertTrue(self.client.loaded)
        self.assertEqual(self.client.permissions, before)
        self.assertEqual(result["delivery"]["turn_id"], "new-turn")
        for _method, params in self.client.calls:
            self.assertFalse(
                set(params)
                & {"approvalPolicy", "sandbox", "sandboxPolicy", "permissions", "config"}
            )

    def test_unloaded_old_or_unknown_server_never_changes_settings(self):
        job = self.due()
        self.client.loaded = False
        self.client.initialize_result = {"userAgent": "Codex/0.148.0 (fixture)"}
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertNotIn("thread/resume", [m for m, _ in self.client.calls])
        self.assertFalse(self.client.loaded)
        self.assertIn("saved permission profile", result["delivery"]["error"])

    def test_empty_unloaded_session_is_not_restored_with_default_settings(self):
        job = self.due()
        self.client.loaded, self.client.has_completed_turn = False, False
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertNotIn("thread/resume", [m for m, _ in self.client.calls])
        self.assertIn("saved permission profile", result["delivery"]["error"])

    def test_queue_idle_starts_exact_accepted_input_without_permission_arguments(self):
        job = self.due()
        self.client.calls.clear()
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(
            [m for m, _ in self.client.calls],
            ["thread/queue/add", "thread/read", "thread/turns/list", "thread/queue/start"],
        )
        self.assertEqual(
            self.client.calls[-1][1],
            {"threadId": "existing-thread", "queuedSubmissionId": "native-queue-item"},
        )
        self.assertEqual(result["delivery"]["turn_id"], "new-turn")
        self.assertEqual(result["delivery"]["queue_start"]["status"], "started")
        self.assertEqual(len(self.client.sent), 1)
        self.assertNotIn("thread/resume", [m for m, _ in self.client.calls])

    def test_idle_to_busy_race_keeps_queue_without_interrupting_or_resending(self):
        self.client.start_race = True
        job = self.due()
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(result["delivery"]["status"], "accepted")
        self.assertEqual(self.client.active, "racing-turn")
        self.assertEqual(len(self.client.sent), 1)
        self.assertFalse(self.client.started_queue_ids)
        self.assertNotIn("error", result["delivery"])

    def test_lost_start_reply_never_resubmits_or_restarts_input(self):
        self.client.start_failure = ConnectionError("Reply lost after possible start")
        job = self.due()
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(result["delivery"]["status"], "uncertain")
        before = len(self.client.calls)
        self.client.start_failure = None
        deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(len(self.client.calls), before)
        self.assertEqual(len(self.client.sent), 1)

    def test_definite_start_refusal_retains_queue_and_reports_error(self):
        self.client.start_failure = RpcError({"message": "model provider unavailable"})
        result, _ = deliver(self.store, lambda _p: self.client, self.due()["id"])
        self.assertEqual(result["delivery"]["status"], "accepted")
        self.assertIn("starting it was refused", result["delivery"]["error"])
        self.assertEqual(result["delivery"]["queue_id"], "native-queue-item")

    def test_malformed_start_reply_is_uncertain(self):
        self.client.start_response = {}
        result, _ = deliver(self.store, lambda _p: self.client, self.due()["id"])
        self.assertEqual(result["delivery"]["status"], "uncertain")
        self.assertNotIn("turn_id", result["delivery"])

    def test_crash_during_queue_start_is_not_replayed(self):
        job = self.due()
        job["delivery"].update(
            status="dispatching",
            queue_id="native-queue-item",
            queue_start={"status": "dispatching"},
        )
        self.store.save(job)
        self.client.calls.clear()
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(result["delivery"]["status"], "uncertain")
        self.assertFalse(self.client.calls)

    def test_steer_uses_native_active_turn_and_idle_start(self):
        for active, expected in [("busy-turn", "turn/steer"), (None, "turn/start")]:
            self.client.active = active
            job = self.due(busy="steer")
            self.client.sent.clear()
            result, _ = deliver(self.store, lambda _p: self.client, job["id"])
            method, params = self.client.sent[-1]
            self.assertEqual(method, expected)
            self.assertEqual(params.get("expectedTurnId"), active)
            self.assertEqual(
                set(params) - {"expectedTurnId"}, {"threadId", "input", "clientUserMessageId"}
            )
            self.assertEqual(result["delivery"]["status"], "accepted")

    def test_steer_does_not_load_or_change_permissions(self):
        job = self.due(busy="steer")
        self.client.loaded = False
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual(result["delivery"]["status"], "failed")
        self.assertFalse(self.client.loaded)
        self.assertFalse(self.client.sent)

    def test_lost_reply_or_crash_never_replays_submission(self):
        for crash in (False, True):
            job = self.due()
            if crash:
                job["delivery"]["status"] = "dispatching"
                self.store.save(job)
            else:
                self.client.failure = ConnectionError("Lost after possible admission")
            before = len(self.client.sent)
            result, _ = deliver(self.store, lambda _p: self.client, job["id"])
            self.assertEqual(result["delivery"]["status"], "uncertain")
            self.client.failure = None
            deliver(self.store, lambda _p: self.client, job["id"])
            self.assertEqual(len(self.client.sent) - before, 0 if crash else 1)

    def test_cancel_before_trigger_but_not_after_native_acceptance(self):
        job = self.arrange()
        result, cancelled = cancel(self.store, self.manager, job["id"], thread="existing-thread")
        self.assertTrue(cancelled)
        self.assertEqual(result["delivery"]["status"], "cancelled")
        deliver(self.store, lambda _p: self.client, job["id"])
        self.assertFalse(self.client.sent)
        job = self.due()
        deliver(self.store, lambda _p: self.client, job["id"])
        before = self.store.path(job["id"]).read_bytes()
        removed = list(self.manager.removed)
        _, cancelled = cancel(self.store, self.manager, job["id"])
        self.assertFalse(cancelled)
        self.assertEqual(self.store.path(job["id"]).read_bytes(), before)
        self.assertEqual(self.manager.removed, removed)
        self.assertNotIn("thread/queue/delete", [m for m, _ in self.client.calls])

    def test_pre_submission_connection_retry_is_not_busy_polling(self):
        job = self.due()

        def offline(_p):
            raise ConnectionError("offline")

        for attempt in range(1, 6):
            result, code = deliver(self.store, offline, job["id"])
            self.assertEqual(code, RETRY if attempt < 5 else 0)
        self.assertEqual(result["delivery"]["status"], "failed")
        self.assertFalse(self.client.sent)

    def test_expected_steer_race_rechecks_once(self):
        job = self.due(busy="steer")
        self.client.active, self.client.race = "active", True
        result, _ = deliver(self.store, lambda _p: self.client, job["id"])
        self.assertEqual([m for m, _ in self.client.sent], ["turn/steer", "turn/start"])
        self.assertEqual(result["delivery"]["status"], "accepted")

    def test_waiter_respects_timer_before_native_submission(self):
        job = self.arrange()

        def advance(_delay):
            self.assertFalse(self.client.sent)
            value = self.store.read(job["id"])
            value["scheduled_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            self.store.save(value)

        result = wait_for_job(self.store, lambda _p: self.client, job["id"], sleeper=advance)
        self.assertEqual(result["delivery"]["status"], "accepted")

    def test_frozen_instruction_does_not_depend_on_source_file(self):
        p = self.root / "instructions.md"
        p.write_text("Frozen original instruction.")
        job = self.due(instructions=instruction_text(filename=p))
        p.unlink()
        deliver(self.store, lambda _p: self.client, job["id"])
        self.assertIn("Frozen original instruction.", self.client.sent[0][1]["input"][0]["text"])
