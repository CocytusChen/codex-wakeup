"""Shared CLI/MCP contract and detached delivery over real fixture transports."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_detached_jobs import FakeDaemon

from codex_wakeup.engine import process_identity

try:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
except ImportError:
    Client = None


@unittest.skipIf(Client is None, "optional MCP extra not installed")
class MCPTest(unittest.TestCase):
    @staticmethod
    async def call(client, name, args, thread="caller-a"):
        return await client.call_tool(name, args, meta={"threadId": thread})

    def test_shared_schema_identity_scope_and_cancel_lifecycle(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                daemon = FakeDaemon(root)
                entry = Path(__file__).resolve().parents[1] / "wakeup"
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        str(entry),
                        "--state-dir",
                        str(state),
                        "--socket",
                        str(daemon.path),
                        "mcp",
                    ],
                    env={**os.environ, "CODEX_THREAD_ID": "stale-startup-caller"},
                )
                jobs = {}
                try:
                    async with Client(params) as client:
                        tools = {t.name: t for t in (await client.list_tools()).tools}
                        self.assertEqual(set(tools), {"wakeup", "list_jobs", "cancel_job"})
                        self.assertEqual(
                            set(tools["wakeup"].input_schema["properties"]),
                            {"message", "trigger", "title", "busy", "dry_run"},
                        )
                        self.assertEqual(
                            tools["wakeup"].input_schema["properties"]["busy"]["default"], "queue"
                        )
                        self.assertEqual(
                            tools["wakeup"].input_schema["$defs"]["TimerTrigger"]["properties"][
                                "count"
                            ]["default"],
                            1,
                        )
                        for name, args in [
                            (
                                "wakeup",
                                {
                                    "message": "no identity",
                                    "trigger": {"type": "timer", "after": "1h"},
                                },
                            ),
                            ("list_jobs", {}),
                            ("cancel_job", {"job": "unknown"}),
                        ]:
                            refused = await client.call_tool(name, args)
                            self.assertTrue(refused.is_error)
                            self.assertEqual(
                                refused.structured_content["error"]["code"],
                                "caller_identity_unavailable",
                            )
                        self.assertFalse(state.exists())
                        for bad in [
                            {"thread": "another"},
                            {"resume_sandbox": "danger-full-access"},
                            {"resume_approval": "never"},
                            {"trigger": {"type": "timer", "after": "1h", "count": True}},
                            {"trigger": {"type": "timer", "after": "1h", "count": "10"}},
                        ]:
                            refused = await self.call(
                                client,
                                "wakeup",
                                {
                                    "message": "bad",
                                    "trigger": {"type": "timer", "after": "1h", "count": 10},
                                    **bad,
                                },
                            )
                            self.assertTrue(refused.is_error)
                            self.assertFalse(state.exists())
                        for thread in ("caller-a", "caller-b"):
                            result = await self.call(
                                client,
                                "wakeup",
                                {
                                    "message": "fixture timer",
                                    "trigger": {"type": "timer", "after": "1h", "count": 10},
                                },
                                thread,
                            )
                            self.assertFalse(result.is_error)
                            jobs[thread] = result.structured_content["data"]["job"]["id"]
                        listed = await self.call(client, "list_jobs", {})
                        self.assertEqual(
                            [j["id"] for j in listed.structured_content["data"]["jobs"]],
                            [jobs["caller-a"]],
                        )
                        protected = state / "jobs" / f"{jobs['caller-b']}.json"
                        before = protected.read_bytes()
                        denied = await self.call(client, "cancel_job", {"job": jobs["caller-b"]})
                        self.assertTrue(denied.is_error)
                        self.assertEqual(protected.read_bytes(), before)
                        hidden = await self.call(
                            client,
                            "list_jobs",
                            {"job": jobs["caller-b"], "include_completed": True},
                        )
                        self.assertEqual(hidden.structured_content["data"]["jobs"], [])
                        # The CLI uses the same argument object and result envelope.
                        cli = subprocess.run(
                            [
                                sys.executable,
                                str(entry),
                                "--state-dir",
                                str(state),
                                "list_jobs",
                                "--json",
                                "{}",
                            ],
                            env={**os.environ, "CODEX_THREAD_ID": "caller-a"},
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        self.assertEqual(json.loads(cli.stdout), listed.structured_content)
                        for thread, job in jobs.items():
                            result = await self.call(client, "cancel_job", {"job": job}, thread)
                            self.assertTrue(result.structured_content["data"]["cancelled"])
                        done = await self.call(client, "list_jobs", {})
                        self.assertEqual(done.structured_content["data"]["jobs"], [])
                        history = await self.call(client, "list_jobs", {"include_completed": True})
                        self.assertEqual(history.structured_content["data"]["total"], 1)
                        self.assertFalse(daemon.sent)
                finally:
                    for thread, job in jobs.items():
                        subprocess.run(
                            [
                                sys.executable,
                                str(entry),
                                "--state-dir",
                                str(state),
                                "cancel_job",
                                "--json",
                                json.dumps({"job": job}),
                            ],
                            env={**os.environ, "CODEX_THREAD_ID": thread},
                            capture_output=True,
                            timeout=5,
                        )
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        values = [
                            json.loads(p.read_text()) for p in (state / "jobs").glob("*.json")
                        ]
                        if all(
                            process_identity(v["worker"]["pid"]) != v["worker"]["start_ticks"]
                            for v in values
                        ):
                            break
                        await asyncio.sleep(0.025)
                    daemon.close()

        asyncio.run(exercise())

    def test_repeated_timer_keeps_waiting_after_mcp_disconnect(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                daemon = FakeDaemon(root)
                entry = Path(__file__).resolve().parents[1] / "wakeup"
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        str(entry),
                        "--state-dir",
                        str(state),
                        "--socket",
                        str(daemon.path),
                        "mcp",
                    ],
                )
                job = None
                try:
                    async with Client(params) as client:
                        response = await self.call(
                            client,
                            "wakeup",
                            {
                                "message": "Inspect timer fixture.",
                                "trigger": {"type": "timer", "after": "1s", "count": 3},
                            },
                        )
                        self.assertFalse(response.is_error)
                        job = response.structured_content["data"]["job"]["id"]
                        self.assertEqual(
                            response.structured_content["data"]["job"]["timer"]["remaining"], 3
                        )
                    deadline = time.monotonic() + 7
                    while time.monotonic() < deadline:
                        value = json.loads((state / "jobs" / f"{job}.json").read_text())
                        worker = value["worker"]
                        if value["timer"]["completed"] == 3 and (
                            process_identity(worker["pid"]) != worker["start_ticks"]
                        ):
                            break
                        await asyncio.sleep(0.025)
                    self.assertEqual(value["timer"]["accepted"], 3)
                    self.assertEqual(len(daemon.sent), 3)
                    self.assertTrue(daemon.active)
                    self.assertNotIn("thread/turns/list", daemon.calls)
                finally:
                    if job:
                        from codex_wakeup.api import call

                        call("cancel_job", {"job": job}, thread="caller-a", state_dir=state)
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            value = json.loads((state / "jobs" / f"{job}.json").read_text())
                            worker = value["worker"]
                            if process_identity(worker["pid"]) != worker["start_ticks"]:
                                break
                            await asyncio.sleep(0.025)
                    daemon.close()

        asyncio.run(exercise())

    def test_command_survives_mcp_disconnect_and_native_queue_acceptance_ends_job(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                daemon = FakeDaemon(root)
                entry = Path(__file__).resolve().parents[1] / "wakeup"
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        str(entry),
                        "--state-dir",
                        str(state),
                        "--socket",
                        str(daemon.path),
                        "mcp",
                    ],
                )
                try:
                    async with Client(params, mode="legacy") as client:
                        result = await self.call(
                            client,
                            "wakeup",
                            {
                                "message": "fixture complete",
                                "trigger": {
                                    "type": "command",
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "import time; time.sleep(.3); "
                                        "open('effect','w').write('once'); print('small result')",
                                    ],
                                    "cwd": tmp,
                                },
                            },
                        )
                        self.assertFalse(result.is_error)
                        job = result.structured_content["data"]["job"]["id"]
                    deadline = time.monotonic() + 7
                    while time.monotonic() < deadline:
                        value = json.loads((state / "jobs" / f"{job}.json").read_text())
                        worker = value.get("worker", {})
                        if (
                            value["delivery"]["status"] == "accepted"
                            and process_identity(worker["pid"]) != worker["start_ticks"]
                        ):
                            break
                        await asyncio.sleep(0.025)
                    self.assertEqual(value["delivery"]["status"], "accepted")
                    self.assertEqual((root / "effect").read_text(), "once")
                    self.assertTrue(daemon.active)
                    self.assertEqual(len(daemon.sent), 1)
                    self.assertIn("small result", daemon.sent[0]["params"]["input"][0]["text"])
                    self.assertEqual(daemon.sent[0]["method"], "thread/queue/add")
                    self.assertNotIn("thread/turns/list", daemon.calls)
                finally:
                    daemon.close()

        asyncio.run(exercise())
