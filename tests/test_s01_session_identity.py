"""Regression tests for S-01 — ``/send`` must derive sender identity
exclusively from the SSE session registry, never from the request body.

Pre-fix, ``/send`` read ``device_id``/``device_name``/``sender`` from the
JSON body and trusted them, so any caller holding a valid session token
could forge messages attributed to another device. ``/rename`` already
enforces ``session_dev_id == body dev_id`` — these tests pin the same
expectation on ``/send``.

Run with:
    python3 -m unittest tests.test_s01_session_identity -v
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
        try: os.kill(pid, 0)
        except OSError: return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def capture_handshake(port: int, pid: int, pid_query: str,
                     display_name: str, timeout: float = 4.0):
    if not wait_for_listen(port, pid, timeout):
        return None, None, None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request(
            "GET",
            f"/events?type=pc&name={display_name}&pid={pid_query}",
            headers={"Accept": "text/event-stream"},
        )
        resp = conn.getresponse()
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = resp.read(256)
            if not chunk: break
            buf += chunk
            while b"\n\n" in buf:
                rec, buf = buf.split(b"\n\n", 1)
                text = rec.decode("utf-8", "replace")
                if text.startswith("event: device_id"):
                    for line in text.splitlines():
                        if line.startswith("data: "):
                            payload = json.loads(line[len("data: "):])
                            return (
                                payload.get("device_id"),
                                payload.get("session_token"),
                                payload.get("name"),
                            )
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
    return None, None, None


def post_send(port: int, session_token: str, body: dict, timeout: float = 5.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Feidi-Session": session_token,
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def post_send_raw(port: int, session_token: str, body: dict):
    """Like post_send but returns (status_code, body_bytes) so we can
    assert 4xx responses without triggering ``fail()`` machinery."""
    try:
        r = post_send(port, session_token, body)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_chunk(port: int, session_token: str, dev_id: str,
               transfer_id: str, chunk_index: int, total: int, body: bytes,
               sender: str = "pc", device_name: str | None = None):
    payload = {
        "sender": sender,
        "device_name": device_name or "TestBot",
        "device_id": dev_id,
        "target_id": None,
        "transfer_id": transfer_id,
        "chunk_index": chunk_index,
        "total_chunks": total,
        "chunk_data": base64.b64encode(body).decode(),
        "file_info": {"name": "sm.bin", "size": len(body) * total,
                      "mime": "application/octet-stream"},
    }
    return post_send_raw(port, session_token, payload)


class FeidiSessionTestCase(unittest.TestCase):
    """Spawn a fresh server and capture the SSE handshake so each test
    has a session token tied to a known real device identity."""

    LOG_DIR = os.path.join(HERE, "_logs", "s01")

    def setUp(self):
        self.port = free_port()
        os.makedirs(self.LOG_DIR, exist_ok=True)
        self.stdout_path = os.path.join(self.LOG_DIR, "server.out.log")
        self.stderr_path = os.path.join(self.LOG_DIR, "server.err.log")
        for p in (self.stdout_path, self.stderr_path):
            open(p, "w").close()
        self._stdout_fh = open(self.stdout_path, "w")
        self._stderr_fh = open(self.stderr_path, "w")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "transfer.py",
                 "--port", str(self.port), "--bind", "127.0.0.1",
                 "--no-browser"],
                cwd=ROOT,
                stdout=self._stdout_fh, stderr=self._stderr_fh,
            )
        except Exception:
            self._stdout_fh.close(); self._stderr_fh.close()
            raise
        if not wait_for_listen(self.port, self.proc.pid):
            self._kill_proc()
            self.fail("server did not start")

    def tearDown(self):
        self._kill_proc()
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh:
                try: fh.close()
                except OSError: pass

    def _kill_proc(self):
        if getattr(self, "proc", None) is not None:
            try:
                self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def open_session(self, pid_suffix: str, name: str = "TestBot"):
        """Capture device_id + session_token + registered display name."""
        pid_q = f"s01-test-pid-{pid_suffix}-{uuid.uuid4().hex[:6]}"
        dev_id, token, server_name = capture_handshake(
            self.port, self.proc.pid, pid_q, name, timeout=4.0)
        if not token:
            self.fail("SSE handshake returned no session_token")
        return dev_id, token, server_name or name


class TestTextSendIdentityBinding(FeidiSessionTestCase):
    """``/send`` for plain text must derive sender/device_name from
    the session, not trust the body."""

    def test_consistent_body_is_accepted(self):
        dev_id, token, server_name = self.open_session("tc1")
        status, body = post_send_raw(self.port, token, {
            "sender": "pc",
            "device_name": server_name,
            "device_id": dev_id,
            "text": "hello with valid identity",
        })
        self.assertEqual(status, 200,
                         f"valid identity should be accepted: body={body[:200]!r}")
        data = json.loads(body)
        self.assertTrue(data.get("ok"), f"expected ok, got {data!r}")

    def test_missing_body_identity_uses_session_default(self):
        """Body without ``device_name``/``device_id``/``sender`` should
        fall back to session-derived values and still succeed."""
        dev_id, token, _ = self.open_session("tc2")
        status, body = post_send_raw(self.port, token, {
            "text": "no identity fields, please use session",
        })
        self.assertEqual(status, 200,
                         f"omitted identity fields should default: body={body[:200]!r}")

    def test_forged_device_id_rejected_403(self):
        """Body ``device_id`` different from session's real id → 403."""
        dev_id, token, _ = self.open_session("tc3")
        status, body = post_send_raw(self.port, token, {
            "sender": "pc",
            "device_name": "Forged",
            "device_id": "attacker-forged-id",
            "text": "this should be rejected",
        })
        self.assertEqual(status, 403,
                         f"forged device_id must yield 403, body={body[:200]!r}")
        payload = json.loads(body)
        self.assertIn(
            "device_id", str(payload.get("error", "")).lower() + str(payload),
            "403 body should mention the offending field",
        )

    def test_forged_device_name_rejected_403(self):
        dev_id, token, _ = self.open_session("tc4")
        status, body = post_send_raw(self.port, token, {
            "sender": "pc",
            "device_name": "PretendToBeSomeoneElse",
            "device_id": dev_id,
            "text": "this should be rejected",
        })
        self.assertEqual(status, 403,
                         f"forged device_name must yield 403, body={body[:200]!r}")

    def test_forged_sender_rejected_403(self):
        """Body ``sender`` not matching the SSE type (pc/mobile) → 403."""
        # handshake used type=pc, so claiming sender="mobile" is forged
        dev_id, token, server_name = self.open_session("tc5")
        status, body = post_send_raw(self.port, token, {
            "sender": "mobile",
            "device_name": server_name,
            "device_id": dev_id,
            "text": "this should be rejected",
        })
        self.assertEqual(status, 403,
                         f"forged sender type must yield 403, body={body[:200]!r}")


class TestChunkSendIdentityBinding(FeidiSessionTestCase):
    """The chunk branch in ``/send`` has its own ownership check
    (``ct['device_id'] != dev_id`` returns 403) but that compares a body
    field against in-memory state — fixing S-01 makes it use the session
    registry up front and a body mismatch 403s before any chunk file is
    written."""

    def test_chunk_post_with_forged_device_id_rejected(self):
        dev_id, token, server_name = self.open_session("ck1")
        transfer_id = f"s01_ck1_{uuid.uuid4().hex[:8]}"
        # Use the REAL sender so the only mismatch is device_id
        status, body = post_chunk(
            self.port, token, "forged-chunk-attacker", transfer_id,
            0, 1, b"\x42" * 256,
            sender="pc", device_name=server_name,
        )
        self.assertEqual(
            status, 403,
            f"forged device_id in chunk POST must yield 403, body={body[:200]!r}",
        )

    def test_chunk_post_with_consistent_identity_accepted(self):
        dev_id, token, server_name = self.open_session("ck2")
        transfer_id = f"s01_ck2_{uuid.uuid4().hex[:8]}"
        status, body = post_chunk(
            self.port, token, dev_id, transfer_id,
            0, 1, b"\x42" * 256,
            sender="pc", device_name=server_name,
        )
        self.assertEqual(
            status, 200,
            f"consistent chunk POST should succeed, body={body[:200]!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
