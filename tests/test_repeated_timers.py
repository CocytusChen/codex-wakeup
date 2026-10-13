"""Finite timer schedules, per-occurrence delivery and supported crash cuts."""

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_wakeup import FakeClient, FakeManager

from codex_wakeup.api import job_summary, validate
from codex_wakeup.engine import (
    RETRY,
    JobStore,
    ToolError,
    cancel,
    deliver,
    is_active_job,
    schedule,
)
from codex_wakeup.rpc import RpcError


class QueuedClient(FakeClient):
    def __init__(self, cwd):
        super().__init__(cwd)
        self.queued, self.deleted = [], []
        self.list_failure, self.delete_failure = None, None
        self.delete_race = False

    def rpc(self, method, params):
        if method == "thread/queue/list":
            self.calls.append((method, params))
            if self.list_failure:
                raise self.list_failure
            offset, limit = int(params.get("cursor", 0)), params["limit"]
            end = offset + limit
            return {
                "data": self.queued[offset:end],
                "nextCursor": str(end) if end < len(self.queued) else None,
            }
        if method == "thread/queue/delete":
            self.calls.append((method, params))
            queue_id = params["queuedSubmissionId"]
            found = next((q for q in self.queued if q["id"] == queue_id), None)
            if found:
                self.queued.remove(found)
            if self.delete_failure:
                raise self.delete_failure
            if found and not self.delete_race:
                self.deleted.append(found)
            return {"deleted": bool(found) and not self.delete_race}
        result = super().rpc(method, params)
        if method == "thread/queue/add":
            entry = result["queuedSubmission"]
            entry["id"] = f"queue-{len(self.sent)}"
            self.queued.append(entry)
        return result


class RepeatedTimerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / "state")
        self.client = FakeClient(str(self.root))
        self.manager = FakeManager()
        self.factory = lambda _path: self.client

    def arrange(self, **kwargs):
        options = dict(
            thread="existing-thread",
            socket_path=self.root / "control.sock",
            instructions="Inspect progress.",
            after="1h",
            count=3,
        )
        options.update(kwargs)
        return schedule(self.store, self.manager, self.factory, **options)

    def due(self, count=3, **kwargs):
        job = self.arrange(count=count, **kwargs)
        job["scheduled_at"] = (
            datetime.now(UTC).replace(microsecond=0) - timedelta(hours=count)
        ).isoformat()
        self.store.save(job)
        return job

    def fire(self, job):
        return deliver(self.store, self.factory, job["id"])[0]

    @staticmethod
    def receipts(job):
        directory = Path(job["timer"]["receipts_directory"])
        return [
            json.loads(p.read_text())
            for p in sorted(directory.glob("*.json"), key=lambda p: int(p.stem))
        ]

    def test_invalid_count_or_absolute_repetition_creates_no_worker(self):
        for count in [0, -1, True, "10", 2.5]:
            with self.subTest(count=count):
                with self.assertRaises(ToolError):
                    self.arrange(count=count)
                with self.assertRaises(ToolError):
                    validate(
                        "wakeup",
                        {
                            "message": "fixture",
                            "trigger": {
                                "type": "timer",
                                "after": "1h",
                                "count": count,
                            },
                        },
                    )
        with self.assertRaises(ToolError):
            self.arrange(after=None, at="2030-01-01T00:00:00Z", count=2)
        with self.assertRaises(ToolError):
            self.arrange(count=10**1000)
        self.assertFalse(self.manager.installed)
        self.assertFalse(self.store.root.exists())

    def test_dry_run_validates_series_without_creating_state(self):
        job = self.arrange(count=10, dry_run=True)
        self.assertEqual(job_summary(job)["timer"]["remaining"], 10)
        self.assertFalse(self.manager.installed)
        self.assertFalse(self.store.root.exists())

    def test_ten_reminders_use_fixed_grid_and_distinct_submission_ids(self):
        job = self.due(count=10)
        first_at = datetime.fromisoformat(job["scheduled_at"])
        for index in range(1, 11):
            job = self.fire(job)
            self.assertEqual(job["timer"]["completed"], index)
            self.assertEqual(is_active_job(job), index < 10)
            if index < 10:
                self.assertEqual(
                    datetime.fromisoformat(job["scheduled_at"]),
                    first_at + timedelta(hours=index),
                )
        ids = [params["clientUserMessageId"] for _, params in self.client.sent]
        self.assertEqual(ids, [f"{job['id']}-{i}" for i in range(1, 11)])
        self.assertEqual(len(self.receipts(job)), 10)
        for i, (_, params) in enumerate(self.client.sent, 1):
            self.assertIn(f"Reminder {i} of 10", params["input"][0]["text"])
        summary = job_summary(job)
        self.assertEqual(summary["timer"]["accepted"], 10)
        self.assertEqual(summary["timer"]["remaining"], 0)
        self.assertIsNone(summary["timer"]["next_at"])
        self.assertFalse(cancel(self.store, self.manager, job["id"])[1])
        self.fire(job)
        self.assertEqual(len(self.client.sent), 10)

    def test_cancel_after_first_reminder_preserves_receipt_and_cancels_remainder(self):
        job = self.fire(self.due())
        job, cancelled = cancel(self.store, self.manager, job["id"], thread="existing-thread")
        self.assertTrue(cancelled)
        self.assertEqual(job["timer"]["completed"], 1)
        self.assertEqual(job["timer"]["cancelled"], 2)
        self.assertEqual(job_summary(job)["timer"]["remaining"], 0)
        self.assertEqual(len(self.receipts(job)), 1)
        self.assertFalse(is_active_job(job))
        self.fire(job)
        self.assertEqual(len(self.client.sent), 1)
        self.assertTrue(cancel(self.store, self.manager, job["id"])[1])

    def test_uncertain_reminder_is_not_replayed_and_later_times_continue(self):
        self.client.failure = ConnectionError("Reply lost after submission")
        job = self.fire(self.due())
        self.assertEqual(job["timer"]["uncertain"], 1)
        self.client.failure = None
        job = self.fire(self.fire(job))
        self.assertEqual(job["timer"]["accepted"], 2)
        self.assertEqual(len(self.client.sent), 3)
        self.assertEqual(self.receipts(job)[0]["delivery"]["status"], "uncertain")

    def test_rejected_reminder_does_not_end_the_remaining_schedule(self):
        self.client.failure = RpcError({"message": "Rejected fixture input"})
        job = self.fire(self.due())
        self.assertEqual(job["timer"]["failed"], 1)
        self.client.failure = None
        job = self.fire(self.fire(job))
        self.assertEqual(job["timer"]["accepted"], 2)
        self.assertFalse(is_active_job(job))

    def test_crash_after_receipt_before_progress_does_not_repeat_accepted_input(self):
        original = self.due()
        stale = copy.deepcopy(original)
        self.fire(original)
        stale["delivery"]["status"] = "dispatching"
        self.store.save(stale)
        job = self.fire(stale)
        self.assertEqual(job["timer"]["accepted"], 1)
        self.assertEqual(job["timer"]["uncertain"], 0)
        self.assertEqual(len(self.client.sent), 1)
        job = self.fire(self.fire(job))
        self.assertEqual(len(self.client.sent), 3)
        self.assertEqual(job["timer"]["completed"], 3)

    def test_crash_without_receipt_consumes_unknown_occurrence_without_replay(self):
        job = self.due()
        job["delivery"]["status"] = "dispatching"
        self.store.save(job)
        job = self.fire(job)
        self.assertEqual(job["timer"]["uncertain"], 1)
        self.assertFalse(self.client.sent)
        job = self.fire(self.fire(job))
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(job["timer"]["completed"], 3)

    def test_cancel_unsent_retry_retains_attempt_and_cancels_future_reminders(self):
        job = self.due()

        def unavailable(_path):
            raise ConnectionError("Fixture server unavailable before submission")

        job, code = deliver(self.store, unavailable, job["id"])
        self.assertEqual(code, RETRY)
        job, cancelled = cancel(self.store, self.manager, job["id"])
        self.assertTrue(cancelled)
        self.assertEqual(job["timer"]["failed"], 1)
        self.assertEqual(job["timer"]["cancelled"], 2)
        self.assertFalse(self.client.sent)


class ReplacementTest(unittest.TestCase):
    arrange = RepeatedTimerTest.arrange
    due = RepeatedTimerTest.due
    fire = RepeatedTimerTest.fire
    receipts = staticmethod(RepeatedTimerTest.receipts)

    def setUp(self):
        RepeatedTimerTest.setUp(self)
        self.client = QueuedClient(str(self.root))
        self.factory = lambda _path: self.client

    def test_new_reminder_replaces_only_older_pending_same_job(self):
        job = self.due(count=10, replace_pending=True)
        unrelated = {"id": "user-input", "clientUserMessageId": "ordinary-user"}
        other_job = {"id": "other-job", "clientUserMessageId": "wake-other-1"}
        future = {"id": "future", "clientUserMessageId": f"{job['id']}-9"}
        lookalike = {"id": "leading-zero", "clientUserMessageId": f"{job['id']}-01"}
        self.client.queued.extend([unrelated, other_job, future, lookalike])
        for _ in range(5):
            job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 5)
        self.assertEqual(job["timer"]["replaced"], 4)
        self.assertEqual(
            self.client.queued,
            [
                unrelated,
                other_job,
                future,
                lookalike,
                {
                    "id": "queue-5",
                    "clientUserMessageId": f"{job['id']}-5",
                    "input": self.client.sent[-1][1]["input"],
                },
            ],
        )
        for _, params in self.client.calls:
            self.assertEqual(params["threadId"], "existing-thread")
        for _, params in self.client.sent:
            self.assertNotIn("Target:", params["input"][0]["text"])
        self.assertIsNone(self.client.active)

    def test_default_retains_all_reminders_without_deletion(self):
        job = self.due()
        for _ in range(3):
            job = self.fire(job)
        self.assertEqual(len(self.client.queued), 3)
        self.assertEqual(job["timer"]["replaced"], 0)
        self.assertFalse(self.client.deleted)

    def test_already_dispatched_old_reminder_is_not_interrupted(self):
        job = self.fire(self.due(replace_pending=True))
        self.client.queued.clear()
        self.client.active = "still-running-first-reminder"
        job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 2)
        self.assertEqual(job["timer"]["replaced"], 0)
        self.assertEqual(self.client.active, "still-running-first-reminder")
        self.assertNotIn("turn/interrupt", [method for method, _ in self.client.calls])

    def test_delete_dispatch_race_records_not_pending(self):
        job = self.fire(self.due(replace_pending=True))
        self.client.delete_race = True
        job = self.fire(job)
        self.assertEqual(job["timer"]["last_queue_replacement"]["not_pending"], 1)
        self.assertEqual(job["timer"]["replaced"], 0)
        self.assertEqual(job["timer"]["accepted"], 2)

    def test_new_rejection_does_not_remove_prior_queued_reminder(self):
        job = self.fire(self.due(replace_pending=True))
        self.client.failure = RpcError({"message": "New reminder rejected"})
        job = self.fire(job)
        self.assertEqual(len(self.client.queued), 1)
        self.assertFalse(self.client.deleted)
        self.assertEqual(job["timer"]["failed"], 1)

    def test_cleanup_failure_keeps_new_acceptance_and_later_cleans_backlog(self):
        job = self.due(count=5, replace_pending=True)
        self.client.list_failure = ConnectionError("Queue query failed")
        for _ in range(4):
            job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 4)
        self.assertEqual(job["timer"]["last_queue_replacement"]["status"], "failed")
        self.client.list_failure = None
        job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 5)
        self.assertEqual(job["timer"]["replaced"], 4)
        self.assertEqual(len(self.client.queued), 1)
        self.assertEqual(len(self.client.sent), 5)

    def test_lost_delete_response_is_uncertain_without_resending_new_reminder(self):
        job = self.fire(self.due(replace_pending=True))
        self.client.delete_failure = ConnectionError("Delete reply lost")
        job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 2)
        self.assertEqual(job["timer"]["last_queue_replacement"]["status"], "uncertain")
        self.assertEqual(job["timer"]["replaced"], 0)
        receipt = self.receipts(job)[1]["delivery"]
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["queue_replacement"]["pending"]["queue_id"], "queue-1")
        self.client.delete_failure = None
        job = self.fire(job)
        self.assertEqual(job["timer"]["accepted"], 3)
        self.assertEqual(len(self.client.sent), 3)

    def test_paginate_before_deleting_without_touching_other_inputs(self):
        job = self.due(replace_pending=True)
        self.client.list_failure = ConnectionError("Queue query failed")
        job = self.fire(self.fire(job))
        self.client.list_failure = None
        others = [{"id": f"other-{i}", "clientUserMessageId": f"user-{i}"} for i in range(205)]
        self.client.queued[1:1] = others
        self.client.calls.clear()
        job = self.fire(job)
        self.assertEqual(job["timer"]["replaced"], 2)
        self.assertEqual(self.client.queued[:-1], others)
        methods = [method for method, _ in self.client.calls]
        self.assertEqual(
            methods,
            ["thread/queue/add"]
            + ["thread/queue/list"] * 3
            + ["thread/queue/delete"] * 2
            + ["thread/read", "thread/turns/list", "thread/queue/start"],
        )

    def test_invalid_replacement_policy_creates_no_worker(self):
        for options in [
            {"count": 1, "replace_pending": True},
            {"busy": "steer", "replace_pending": True},
            {"replace_pending": "true"},
            {"replace_pending": 1},
        ]:
            with self.subTest(options=options), self.assertRaises(ToolError):
                self.arrange(**options)
        self.assertFalse(self.manager.installed)

    def test_crash_during_cleanup_does_not_resubmit_or_redelete(self):
        job = self.fire(self.due(replace_pending=True))
        job["delivery"] = {
            "status": "accepted",
            "queue_id": "queue-2",
            "queue_replacement": {
                "status": "running",
                "removed": [],
                "not_pending": [],
                "pending": {"queue_id": "queue-1", "client_user_message_id": f"{job['id']}-1"},
            },
        }
        self.store.save(job)
        before = len(self.client.calls)
        job = self.fire(job)
        self.assertEqual(len(self.client.calls), before)
        self.assertEqual(job["timer"]["completed"], 2)
        self.assertEqual(job["timer"]["last_queue_replacement"]["status"], "uncertain")
        self.assertEqual(self.receipts(job)[1]["delivery"]["status"], "accepted")

    def test_uncertain_prior_add_is_removed_by_exact_queued_identity(self):
        job = self.due(replace_pending=True)
        original_rpc = self.client.rpc

        def lost_reply(method, params):
            response = original_rpc(method, params)
            if method == "thread/queue/add":
                raise ConnectionError("Add accepted but reply lost")
            return response

        self.client.rpc = lost_reply
        job = self.fire(job)
        self.assertEqual(job["timer"]["uncertain"], 1)
        self.client.rpc = original_rpc
        job = self.fire(job)
        self.assertEqual(job["timer"]["replaced"], 1)
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.queued[0]["clientUserMessageId"], f"{job['id']}-2")
