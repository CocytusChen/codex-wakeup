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

    def due(self, count=3):
        job = self.arrange(count=count)
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
