"""Bounded JSON-RPC over Codex's Unix WebSocket control socket."""

import base64
import hashlib
import json
import os
import socket
import struct
import time

from . import __version__


class RpcError(Exception):
    """A definite server rejection; distinct from an uncertain transport failure."""

    def __init__(self, error):
        self.error = error
        super().__init__(str(error.get("message", "RPC rejected"))[:1000])


class RpcClient:
    def __init__(self, path, timeout=15, max_bytes=128 * 1024 * 1024):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.reader = None
        self.next_id = 1
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            self.reader = self.sock.makefile("rb")
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n\r\n"
                ).encode()
            )
            headers = self._headers()
            lines = headers.decode("ascii").split("\r\n")
            values = {
                key.lower(): value
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            expected = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            if (
                " 101 " not in lines[0]
                or values.get("sec-websocket-accept", "").strip() != expected
            ):
                raise ConnectionError("WebSocket handshake rejected")
            self.initialize_result = self.rpc(
                "initialize",
                {
                    "clientInfo": {"name": "codex-wakeup", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send(b'{"method":"initialized","params":{}}')
        except Exception:
            self.close()
            raise

    def _headers(self):
        result = bytearray()
        while len(result) < 16384:
            line = self.reader.readline(16385 - len(result))
            if not line:
                raise ConnectionError("WebSocket handshake closed")
            result.extend(line)
            if line == b"\r\n":
                return bytes(result)
        raise ConnectionError("WebSocket handshake exceeds header bound")

    def close(self):
        if self.reader is not None:
            self.reader.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def send(self, payload, opcode=1):
        mask = os.urandom(4)
        length = len(payload)
        header = bytes(
            [0x80 | opcode, 0x80 | (length if length < 126 else 126 if length < 65536 else 127)]
        )
        if length >= 126:
            header += struct.pack("!H" if length < 65536 else "!Q", length)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, size):
        data = self.reader.read(size)
        if len(data) != size:
            raise ConnectionError("WebSocket closed or truncated")
        return data

    def recv(self, deadline):
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC response deadline exceeded")
            self.sock.settimeout(remaining)
            header = self._read(2)
            opcode, length = header[0] & 15, header[1] & 127
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length + len(chunks) > self.max_bytes:
                raise ConnectionError("RPC response exceeds configured byte bound")
            mask = self._read(4) if header[1] & 128 else None
            body = self._read(length)
            if mask:
                body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
            if opcode == 8:
                raise ConnectionError("Server closed the WebSocket")
            if opcode == 9:
                self.send(body, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ConnectionError("Unexpected WebSocket opcode")
            chunks.extend(body)
            if header[0] & 128:
                message = json.loads(chunks)
                if not isinstance(message, dict):
                    raise ConnectionError("RPC response is not an object")
                return message

    def rpc(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        deadline = time.monotonic() + self.timeout
        self.send(json.dumps({"id": request_id, "method": method, "params": params}).encode())
        while True:
            response = self.recv(deadline)
            if response.get("id") == request_id:
                if "error" in response:
                    raise RpcError(response["error"])
                return response["result"]
