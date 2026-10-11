"""Real CLI/worker/process tests against a contained fake Codex control endpoint."""

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_wakeup.engine import process_identity
from codex_wakeup.jobs import identity


class FakeDaemon:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "control.sock"
        self.sent = []
        self.calls = []
        self.active = True
        self.stop = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.path))
        self.socket.listen()
        self.socket.settimeout(0.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.socket.close()

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.socket.accept()
            except TimeoutError:
                continue
            with conn:
                self.handle(conn)

    def handle(self, conn):
        conn.settimeout(3)
        stream = conn.makefile("rb")
        try:
            headers = {}
            while True:
                line = stream.readline()
                if line in (b"\r\n", b""):
                    break
                if b":" in line:
                    key, val = line.split(b":", 1)
                    headers[key.lower()] = val.strip()
            accept = base64.b64encode(
                hashlib.sha1(
                    headers[b"sec-websocket-key"] + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).digest()
            )
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while True:
                header = stream.read(2)
                if not header:
                    return
                size = header[1] & 127
                if size == 126:
                    size = struct.unpack("!H", stream.read(2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", stream.read(8))[0]
                mask = stream.read(4)
                body = stream.read(size)
                message = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(body)))
                if "id" not in message:
                    continue
                method = message["method"]
                self.calls.append(method)
                if method == "thread/read":
                    result = {
                        "thread": {
                            "cwd": str(self.root),
                            "status": {"type": "active" if self.active else "idle"},
                        }
                    }
                elif method == "thread/turns/list":
                    result = {
                        "data": [{"id": "fake-active", "status": "inProgress"}]
                        if self.active
                        else []
                    }
                elif method == "thread/queue/list":
                    result = {"data": []}
                elif method == "thread/queue/add":
                    self.sent.append(message)
                    result = {
                        "queuedSubmission": {
                            "id": "native-" + str(len(self.sent)),
                            "clientUserMessageId": message["params"]["clientUserMessageId"],
                            "input": message["params"]["input"],
                        }
                    }
                elif method == "turn/steer":
                    self.sent.append(message)
                    result = {"turnId": "fake-active"}
                elif method == "turn/start":
                    self.sent.append(message)
                    result = {"turn": {"id": "test-turn-completion"}}
                else:
                    result = {}
                body = json.dumps({"id": message["id"], "result": result}).encode()
                prefix = (
                    bytes([129, len(body)])
                    if len(body) < 126
                    else b"\x81\x7e" + struct.pack("!H", len(body))
                )
                conn.sendall(prefix + body)
        except (OSError, ValueError):
            return
        finally:
            stream.close()


class DetachedJobTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.state = Path(tmp.name), Path(tmp.name) / "state"
        self.daemon = FakeDaemon(self.root)
        self.addCleanup(self.daemon.close)
        self.entry = Path(__file__).resolve().parents[1] / "wakeup"
        self.jobs = []
        self.addCleanup(self.retire_workers)

    def cli(self, operation, arguments=None, thread="test-only"):
        p = subprocess.run(
            [
                sys.executable,
                str(self.entry),
                "--state-dir",
                str(self.state),
                "--socket",
                str(self.daemon.path),
                operation,
                "--json",
                json.dumps(arguments or {}),
            ],
            env={**os.environ, "CODEX_THREAD_ID": thread},
            text=True,
            capture_output=True,
            timeout=10,
        )
        value = json.loads(p.stdout)
        self.assertTrue(value["ok"], value)
        return value["data"]

    def submit(self, trigger, **kwargs):
        data = self.cli(
            "wakeup", {"message": "Inspect fixture result.", "trigger": trigger, **kwargs}
        )
        self.jobs.append(data["job"]["id"])
        return data["job"]["id"]

    def wait_for(self, job, condition):
        end = time.monotonic() + 7
        while time.monotonic() < end:
            value = json.loads((self.state / "jobs" / f"{job}.json").read_text())
            if condition(value):
                return value
            time.sleep(0.025)
        self.fail("Contained worker did not reach expected state.")

    @staticmethod
    def worker_alive(job):
        worker = job.get("worker", {})
        return bool(worker) and process_identity(worker["pid"]) == worker["start_ticks"]

    def retire_workers(self):
        for job in self.jobs:
            self.cli("cancel_job", {"job": job})
            self.wait_for(job, lambda v: not self.worker_alive(v))

    def command(self, code, **options):
        return self.submit(
            {
                "type": "command",
                "argv": [sys.executable, "-c", code],
                "cwd": str(self.root),
                **options,
            }
        )

    def test_cli_rejects_target_and_policy_arguments_before_any_side_effect(self):
        for extra in [
            {"thread": "another"},
            {"resume_sandbox": "danger-full-access"},
            {"resume_approval": "never"},
            {"busy": "defer"},
            {"dry_run": "false"},
        ]:
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.entry),
                    "--state-dir",
                    str(self.state),
                    "--socket",
                    str(self.daemon.path),
                    "wakeup",
                    "--json",
                    json.dumps(
                        {
                            "message": "fixture",
                            "trigger": {"type": "timer", "after": "1h"},
                            **extra,
                        }
                    ),
                ],
                env={**os.environ, "CODEX_THREAD_ID": "test-only"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.daemon.calls)

    def test_cli_requires_host_identity(self):
        env = {k: v for k, v in os.environ.items() if k != "CODEX_THREAD_ID"}
        result = subprocess.run(
            [sys.executable, str(self.entry), "--state-dir", str(self.state), "list_jobs"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "caller_identity_unavailable")
        self.assertFalse(self.state.exists())
        self.assertFalse(self.daemon.calls)

    def test_queue_acceptance_ends_worker_while_target_stays_busy(self):
        job = self.command("open('effect','a').write('once'); print('done')")
        result = self.wait_for(job, lambda v: v["delivery"]["status"] == "accepted")
        self.wait_for(job, lambda v: not self.worker_alive(v))
        self.assertTrue(self.daemon.active)
        self.assertEqual((self.root / "effect").read_text(), "once")
        self.assertEqual(len(self.daemon.sent), 1)
        self.assertEqual(self.daemon.sent[0]["method"], "thread/queue/add")
        self.assertEqual(result["execution"]["exit_code"], 0)
        self.assertNotIn("thread/turns/list", self.daemon.calls)
        self.assertEqual(self.cli("list_jobs", {"job": job})["jobs"], [])
        self.assertFalse(self.cli("cancel_job", {"job": job})["cancelled"])
        self.assertEqual(len(self.daemon.sent), 1)

    def test_timer_cancel_before_fire_never_enqueues(self):
        job = self.submit({"type": "timer", "after": "1h"})
        self.assertEqual(len(self.cli("list_jobs")["jobs"]), 1)
        self.assertTrue(self.cli("cancel_job", {"job": job})["cancelled"])
        self.wait_for(job, lambda v: not self.worker_alive(v))
        self.assertFalse(self.daemon.sent)

    def test_repeated_timer_stays_active_then_exits_after_three_enqueues(self):
        job = self.submit({"type": "timer", "after": "1s", "count": 3})
        self.assertEqual(self.cli("list_jobs", {"job": job})["jobs"][0]["timer"]["remaining"], 3)
        value = self.wait_for(job, lambda v: v["timer"]["completed"] == 3)
        self.wait_for(job, lambda v: not self.worker_alive(v))
        self.assertEqual(value["timer"]["accepted"], 3)
        self.assertEqual(len(self.daemon.sent), 3)
        self.assertEqual(
            [entry["params"]["clientUserMessageId"] for entry in self.daemon.sent],
            [f"{job}-{index}" for index in range(1, 4)],
        )
        self.assertTrue(self.daemon.active)
        self.assertEqual(self.cli("list_jobs", {"job": job})["jobs"], [])

    def test_repeated_timer_can_cancel_after_first_enqueue(self):
        job = self.submit({"type": "timer", "after": "1s", "count": 5})
        self.wait_for(job, lambda v: v["timer"]["completed"] == 1)
        cancelled = self.cli("cancel_job", {"job": job})
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["job"]["timer"]["cancelled"], 4)
        self.wait_for(job, lambda v: not self.worker_alive(v))
        self.assertEqual(len(self.daemon.sent), 1)

    def test_command_cancel_settles_owned_group(self):
        job = self.command("import time; time.sleep(30)")
        running = self.wait_for(job, lambda v: v["execution"]["status"] == "running")
        child = running["execution"]["process"]["pid"]
        self.assertTrue(self.cli("cancel_job", {"job": job})["cancelled"])
        result = self.wait_for(job, lambda v: v["execution"]["status"] == "cancelled")
        self.assertEqual(result["delivery"]["status"], "cancelled")
        self.assertIsNone(identity(child))
        self.assertFalse(self.daemon.sent)

    def test_inline_default_and_complete_spill(self):
        job = self.command("print('x'*1024)")
        value = self.wait_for(job, lambda v: v["delivery"]["status"] == "accepted")
        self.assertEqual(value["trigger"]["output_limit"], 1024)
        path = Path(value["execution"]["output_path"])
        self.assertEqual(path.read_bytes(), b"x" * 1024 + b"\n")
        message = self.daemon.sent[0]["params"]["input"][0]["text"]
        self.assertIn(str(path), message)
        self.assertNotIn("xxxx", message)
        query = self.cli("list_jobs", {"job": job, "include_completed": True})
        self.assertEqual(query["jobs"][0]["delivery"]["status"], "accepted")
        self.assertNotIn("xxxx", json.dumps(query))

    def test_watch_exit_and_cancel_leave_observed_process_alone(self):
        for cancel_first in (False, True):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.6)"])
            try:
                job = self.submit({"type": "process_exit", "pid": child.pid})
                if cancel_first:
                    self.assertTrue(self.cli("cancel_job", {"job": job})["cancelled"])
                    self.wait_for(job, lambda v: not self.worker_alive(v))
                    self.assertIsNone(child.poll())
                else:
                    value = self.wait_for(job, lambda v: v["delivery"]["status"] == "accepted")
                    self.assertIsNone(value["execution"]["exit_code"])
                    self.assertEqual(child.wait(timeout=1), 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=2)

    def test_steer_uses_current_turn_with_no_settings_overrides(self):
        job = self.command("print('steer result')", **{})
        self.wait_for(job, lambda v: v["delivery"]["status"] == "accepted")
        job = self.submit(
            {"type": "command", "argv": [sys.executable, "-c", "pass"], "cwd": str(self.root)},
            busy="steer",
        )
        self.wait_for(job, lambda v: v["delivery"]["status"] == "accepted")
        message = self.daemon.sent[-1]
        self.assertEqual(message["method"], "turn/steer")
        self.assertEqual(message["params"]["expectedTurnId"], "fake-active")
        self.assertEqual(
            set(message["params"]), {"threadId", "expectedTurnId", "input", "clientUserMessageId"}
        )
