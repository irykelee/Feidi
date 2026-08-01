"""Regression tests for C-01 — ``_inflight_bytes`` scoping error.

The bug at HEAD 26b97bc has the do_POST chunk branch reading + assigning the
module-level ``_inflight_bytes`` without a ``global`` declaration. Python
treats the identifier as a fresh local variable, so the first read on a fresh
worker thread raises UnboundLocalError and the handler traceback closes the
socket without sending a JSON response. The same defect exists in
``_load_chunk_states`` and ``_cleanup_stale_chunks``.

These tests run against a real subprocess of ``transfer.py`` to exercise the
unmodified handler code paths. Run with:

    python3 -m unittest discover -s tests -p 'test_c01_*.py' -v

They require:
- Feidi server module not currently bound to the chosen test port
- A POSIX-y environment (signal / subprocess behaviour)
- No pytest dependency — pure stdlib unittest
"""

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_listen(port: int, pid: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def capture_session_token(port: int, timeout: float = 4.0):
    """Connect to /events, read the first ``event: device_id`` record."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request(
            "GET",
            "/events?type=pc&name=TestBot&pid=test-bot-c01-pid",
            headers={"Accept": "text/event-stream"},
        )
        resp = conn.getresponse()
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = resp.read(256)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                rec, buf = buf.split(b"\n\n", 1)
                text = rec.decode("utf-8", "replace")
                if text.startswith("event: device_id"):
                    for line in text.splitlines():
                        if line.startswith("data: "):
                            payload = json.loads(line[len("data: "):])
                            return payload.get("device_id"), payload.get("session_token")
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return None, None


def _build_chunk_payload(dev_id: str, transfer_id: str, chunk_index: int,
                         total_chunks: int, body: bytes) -> dict:
    return {
        "sender": "pc",
        "device_name": "TestBot",
        "device_id": dev_id,
        "target_id": None,
        "transfer_id": transfer_id,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "chunk_data": base64.b64encode(body).decode(),
        "file_info": {
            "name": "sm.bin",
            "size": len(body) * total_chunks,
            "mime": "application/octet-stream",
        },
    }


def post_chunk(port: int, dev_id: str, session_token: str,
               transfer_id: str, chunk_index: int, total_chunks: int, body: bytes):
    payload = _build_chunk_payload(dev_id, transfer_id, chunk_index,
                                   total_chunks, body)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Feidi-Session": session_token,
        },
    )
    return urllib.request.urlopen(req, timeout=5)


def _server_logs(proc: subprocess.Popen):
    """Best-effort flush of the captured stdout/stderr files."""
    logs = {"stdout": "", "stderr": ""}
    for stream_name in ("stdout", "stderr"):
        try:
            with open(getattr(proc, f"{stream_name}_path"), "r",
                      encoding="utf-8", errors="replace") as fh:
                logs[stream_name] = fh.read()
        except OSError:
            pass
    return logs


class FeidiServerProc:
    """Context manager around a sandboxed Feidi subprocess bound to 127.0.0.1."""

    def __init__(self, log_dir: str):
        self.port = free_port()
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.stdout_path = os.path.join(log_dir, "server.out.log")
        self.stderr_path = os.path.join(log_dir, "server.err.log")
        self.proc = None
        self.dev_id = None
        self.session_token = None

    def __enter__(self):
        for p in (self.stdout_path, self.stderr_path):
            with open(p, "w"):
                pass
        # Each server uses a unique transfer_id prefix to avoid collisions
        # with other tests that left ``feidi_chunks`` behind.
        self._stdout_fh = open(self.stdout_path, "w")
        self._stderr_fh = open(self.stderr_path, "w")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "transfer.py",
                 "--port", str(self.port), "--bind", "127.0.0.1", "--no-browser"],
                cwd=ROOT,
                stdout=self._stdout_fh,
                stderr=self._stderr_fh,
            )
        except Exception:
            self._stdout_fh.close()
            self._stderr_fh.close()
            raise
        if not wait_for_listen(self.port, self.proc.pid, timeout=12.0):
            self.__exit__(None, None, None)
            raise RuntimeError(
                f"server did not listen on {self.port}; see {self.stderr_path}")
        self.dev_id, self.session_token = capture_session_token(self.port, timeout=4.0)
        if not self.session_token:
            self.__exit__(None, None, None)
            raise RuntimeError("SSE handshake did not return a session_token")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                setattr(self, fh_attr, None)


class TestInflightScoping(unittest.TestCase):
    """P0 C-01: chunk POST path must not raise UnboundLocalError and must
    return a JSON response on the happy path."""

    LOG_DIR = os.path.join(HERE, "_logs", "c01")

    def setUp(self):
        self.ctx = FeidiServerProc(self.LOG_DIR)
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def _server_has_unbound(self) -> bool:
        for path in (self.ctx.stderr_path, self.ctx.stdout_path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except OSError:
                continue
            if "UnboundLocalError" in txt and "_inflight_bytes" in txt:
                return True
        return False

    def test_single_chunk_post_returns_json_ok(self):
        """A single chunk POST should succeed (HTTP 200) and return a
        JSON body with ``ok: True`` and the requested chunk in
        ``received``. BEFORE fix, the handler raises UnboundLocalError and
        the socket closes without a response (``RemoteDisconnected``)."""
        body = b"\x42" * 1024
        # unique transfer_id avoids collision with stale state files left
        # by prior test runs / dev sessions on this checkout
        transfer_id = f"t01_{uuid.uuid4().hex[:8]}"
        try:
            resp = post_chunk(
                self.ctx.port, self.ctx.dev_id, self.ctx.session_token,
                transfer_id, 0, 1, body,
            )
        except (ConnectionResetError, BrokenPipeError, urllib.error.URLError) as e:
            self.fail(
                f"chunk POST failed before any HTTP response: "
                f"{type(e).__name__}: {e}; "
                f"server likely raised UnboundLocalError on _inflight_bytes"
            )
        self.assertEqual(resp.status, 200,
                         "chunk POST should return 200 on happy path")
        data = json.loads(resp.read().decode("utf-8", "replace"))
        self.assertTrue(data.get("ok"), f"expected ok, got {data!r}")
        self.assertIn(0, data.get("received", []),
                      f"chunk index 0 should be in received list, got {data!r}")
        self.assertTrue(data.get("complete"),
                        f"single-chunk transfer should be complete, got {data!r}")
        # And the server must not have tracebacked on _inflight_bytes
        self.assertFalse(self._server_has_unbound(),
                         "server raised UnboundLocalError on _inflight_bytes")

    def test_multi_chunk_post_completes(self):
        """A 2-chunk transfer sends chunk 0, then chunk 1; second response
        should be ``complete: True`` and include both indices. Both
        responses must be valid JSON (no handler traceback)."""
        body = b"\x33" * 512
        transfer_id = f"t02_{uuid.uuid4().hex[:8]}"
        try:
            r0 = post_chunk(
                self.ctx.port, self.ctx.dev_id, self.ctx.session_token,
                transfer_id, 0, 2, body,
            )
            data0 = json.loads(r0.read().decode("utf-8", "replace"))
            self.assertEqual(r0.status, 200)
            self.assertFalse(data0.get("complete"),
                             f"first chunk should not yet complete: {data0!r}")

            r1 = post_chunk(
                self.ctx.port, self.ctx.dev_id, self.ctx.session_token,
                transfer_id, 1, 2, body,
            )
            data1 = json.loads(r1.read().decode("utf-8", "replace"))
        except (ConnectionResetError, BrokenPipeError, urllib.error.URLError) as e:
            self.fail(
                f"multi-chunk POST failed: {type(e).__name__}: {e}; "
                f"server likely raised UnboundLocalError on _inflight_bytes"
            )
        self.assertEqual(r1.status, 200)
        self.assertTrue(data1.get("complete"),
                        f"second chunk should complete the transfer: {data1!r}")
        self.assertEqual(set(data1.get("received", [])), {0, 1})
        self.assertFalse(self._server_has_unbound(),
                         "server raised UnboundLocalError on _inflight_bytes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
