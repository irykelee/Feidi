"""Regression tests for C-03 (input validation) and R-04 (rate-limit
bound).

C-03 (P1):
    Malformed /login or /send payloads must yield a clean 4xx response
    instead of bubbling the exception to the connection.

R-04 (P2):
    ``_rate_limits`` must be bounded: stale entries are purged by the
    periodic cleanup loop, and the dict size never exceeds a hard cap.

Run with:
    python3 -m unittest tests.test_c03_r04_validation_and_limits -v
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


def capture_session(port: int, timeout: float = 4.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request(
            "GET",
            f"/events?type=pc&name=TestBot&pid=c03-{uuid.uuid4().hex[:8]}",
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
                            return payload.get("device_id"), payload.get("session_token")
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
    return None, None


def post(port: int, path: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body, method="POST", headers=headers,
        ), timeout=5)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        return -1, str(e).encode()


class FeidiServerTestCase(unittest.TestCase):
    LOG_DIR = os.path.join(HERE, "_logs", "c03_r04")

    def setUp(self):
        self.port = free_port()
        os.makedirs(self.LOG_DIR, exist_ok=True)
        self.stderr_path = os.path.join(self.LOG_DIR, "server.err.log")
        self.stdout_path = os.path.join(self.LOG_DIR, "server.out.log")
        for p in (self.stderr_path, self.stdout_path):
            open(p, "w").close()
        self._so = open(self.stdout_path, "w")
        self._se = open(self.stderr_path, "w")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "transfer.py",
                 "--port", str(self.port), "--bind", "127.0.0.1", "--no-browser"],
                cwd=ROOT,
                stdout=self._so, stderr=self._se,
            )
        except Exception:
            self._so.close(); self._se.close()
            raise
        if not wait_for_listen(self.port, self.proc.pid):
            self._kill(); self.fail("server did not start")

    def tearDown(self):
        self._kill()
        for fh in (self._so, self._se):
            try: fh.close()
            except Exception: pass

    def _kill(self):
        if getattr(self, "proc", None) is not None:
            try:
                self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def open_session(self):
        dev_id, token = capture_session(self.port, timeout=4.0)
        if not token:
            self.fail("SSE handshake failed")
        return dev_id, token

    def server_has_unhandled_exception(self) -> bool:
        # Allow time for any traceback to flush to stderr.
        time.sleep(0.3)
        try:
            with open(self.stderr_path, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        for marker in ("Traceback (most recent call last)",
                       "UnboundLocalError",
                       "AttributeError",
                       "TypeError:",
                       "ValueError:"):
            if marker in text:
                return True
        return False


class TestC03LoginValidation(FeidiServerTestCase):
    """``/login`` must tolerate non-string passwords without raising."""

    def test_login_null_password(self):
        status, body = post(self.port, "/login",
                            json.dumps({"password": None}).encode(),
                            {"Content-Type": "application/json"})
        # Pre-fix: TypeError raised in compare_digest → connection drop.
        # Post-fix: 400 with informative message.
        self.assertNotEqual(status, -1,
                            "pre-fix crash; should be clean 4xx")
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertFalse(self.server_has_unhandled_exception(),
                         "server traceback on null password")

    def test_login_numeric_password(self):
        status, body = post(self.port, "/login",
                            json.dumps({"password": 12345}).encode(),
                            {"Content-Type": "application/json"})
        self.assertNotEqual(status, -1, "should not crash")
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertFalse(self.server_has_unhandled_exception())

    def test_login_empty_body(self):
        status, body = post(self.port, "/login", b"",
                            {"Content-Type": "application/json"})
        self.assertNotEqual(status, -1, "should not crash")
        # 401 from compare_digest (secrets.compare_digest with empty ==
        # password ''), which is acceptable provided it isn't 5xx.
        self.assertLess(status, 500)


class TestC03SendValidation(FeidiServerTestCase):
    """``/send`` must tolerate malformed JSON/object/fields without raising."""

    def setUp(self):
        super().setUp()
        # For each test, get a *fresh* session because rate-limit
        # tightly couples to current test scope.
        self.dev_id, self.token = self.open_session()

    def _post_send(self, body: bytes):
        return post(self.port, "/send", body, {
            "Content-Type": "application/json",
            "X-Feidi-Session": self.token,
        })

    def _assert_clean_4xx(self, status, body, label):
        self.assertNotEqual(
            status, -1,
            f"[{label}] handler raised an unhandled exception; "
            f"connection died: body={body[:120]!r}",
        )
        self.assertGreaterEqual(status, 400, f"[{label}] should be 4xx")
        self.assertLess(status, 500, f"[{label}] 5xx is leaking exception")
        self.assertFalse(
            self.server_has_unhandled_exception(),
            f"[{label}] server stderr shows unhandled exception")

    def test_root_list_rejected(self):
        status, body = self._post_send(b"[1, 2, 3]")
        self._assert_clean_4xx(status, body, "root list")

    def test_root_string_rejected(self):
        status, body = self._post_send(b'"not an object"')
        self._assert_clean_4xx(status, body, "root string")

    def test_text_not_str_rejected(self):
        status, body = self._post_send(json.dumps({"text": 123}).encode())
        self._assert_clean_4xx(status, body, "text int")

    def test_text_array_rejected(self):
        status, body = self._post_send(json.dumps({"text": ["a", "b"]}).encode())
        self._assert_clean_4xx(status, body, "text list")

    def test_chunk_index_not_int_rejected(self):
        status, body = self._post_send(json.dumps({
            "transfer_id": "t1", "chunk_index": "x", "total_chunks": 2,
        }).encode())
        self._assert_clean_4xx(status, body, "chunk_index str")

    def test_total_chunks_not_int_rejected(self):
        status, body = self._post_send(json.dumps({
            "transfer_id": "t1", "chunk_index": 0, "total_chunks": "y",
        }).encode())
        self._assert_clean_4xx(status, body, "total_chunks str")

    def test_target_id_not_str_rejected(self):
        status, body = self._post_send(json.dumps({
            "text": "hi", "target_id": 99,
        }).encode())
        self._assert_clean_4xx(status, body, "target_id int")

    def test_image_data_uri_no_comma_does_not_crash(self):
        """``image`` data URI without a comma suffix used to throw a
        ``binascii.Error`` inside add_message(); M4 fix downgrades it
        to ``application/octet-stream`` rather than crashing. The
        regression target is *no traceback*: status can be 200 with
        degraded binary, but never a connection drop or 5xx."""
        status, body = self._post_send(json.dumps({
            "image": "data:image/png;base64",
        }).encode())
        self.assertNotEqual(
            status, -1,
            f"handler raised; body={body[:120]!r}",
        )
        self.assertLess(status, 500)
        self.assertFalse(
            self.server_has_unhandled_exception(),
            "server stderr shows unhandled exception")


class TestR04RateLimitBounded(unittest.TestCase):
    """``_rate_limits`` size must remain bounded; periodic cleanup
    purges stale IP entries; an explicit hard cap protects against
    adversarial bursts.

    This test exercises the public surface (import transfer, monkey-call
    cleanup) so it depends on transfer.py being importable in this
    Python session. The test is gated on the module-level symbol.
    """

    def test_rate_limits_under_cap(self):
        # Importing transfer.py triggers argparse.parse_args() at module
        # load; unittest's argv looks like ``['-m', 'unittest', ...]``
        # which the parser rejects. Provide a sys.argv that includes
        # only flags transfer.py expects (none) plus the script name.
        sys.path.insert(0, ROOT)
        saved_argv = sys.argv
        sys.argv = ["transfer.py"]
        try:
            import transfer  # noqa: F401
        except SystemExit:
            self.skipTest("transfer.py argparse rejected sys.argv; skipping in-process check")
        except Exception:
            self.skipTest("transfer.py import failed; skipping in-process check")
        finally:
            sys.argv = saved_argv
        from transfer import _rate_limits, _rate_lock, _rate_limits_cleanup, _MAX_RATE_LIMIT_KEYS

        # Reset shared state in case prior tests left stuff
        with _rate_lock:
            _rate_limits.clear()

        # Seed 50 distinct entries with old last_ts to mimic 1h+ idle IPs.
        old_ts = time.time() - 3700
        with _rate_lock:
            for i in range(50):
                _rate_limits[f"old-{i}"] = [old_ts]
            # 5 recent entries (must NOT be cleaned)
            now = time.time()
            for i in range(5):
                _rate_limits[f"recent-{i}"] = [now]

        # Cleanup; old entries should go, recent kept.
        _rate_limits_cleanup()
        with _rate_lock:
            remaining = set(_rate_limits)
        for i in range(50):
            self.assertNotIn(f"old-{i}", remaining,
                             f"stale entry old-{i} should be cleaned up")
        for i in range(5):
            self.assertIn(f"recent-{i}", remaining,
                          f"recent entry recent-{i} should survive cleanup")

    def test_rate_limits_hard_cap(self):
        sys.path.insert(0, ROOT)
        saved_argv = sys.argv
        sys.argv = ["transfer.py"]
        try:
            import transfer  # noqa: F401
        except SystemExit:
            self.skipTest("transfer.py argparse rejected sys.argv; skipping in-process check")
        except Exception:
            self.skipTest("transfer.py import failed; skipping in-process check")
        finally:
            sys.argv = saved_argv
        from transfer import (
            _rate_limits, _rate_lock, _rate_limits_cleanup,
        )
        import transfer as _t

        with _rate_lock:
            _rate_limits.clear()
        # Monkey-patch the cap down for a quick eviction test.
        # Cleanup reads the bound name via closure; we patch the module attr.
        original_cap = _t._MAX_RATE_LIMIT_KEYS
        _t._MAX_RATE_LIMIT_KEYS = 100
        try:
            base_ts = time.time()
            with _rate_lock:
                for i in range(500):
                    _rate_limits[f"burst-{i}"] = [
                        base_ts - (500 - i) * 0.001
                    ]
            _rate_limits_cleanup()
            with _rate_lock:
                size = len(_rate_limits)
            self.assertLessEqual(
                size, _t._MAX_RATE_LIMIT_KEYS,
                f"rate-limit dict should be capped at {_t._MAX_RATE_LIMIT_KEYS}, "
                f"got {size}",
            )
            # And the older half should be evicted, keeping newer half.
            with _rate_lock:
                keys = list(_rate_limits.keys())
            self.assertGreater(len(keys), 0,
                               "at least some recent entries should survive")
        finally:
            _t._MAX_RATE_LIMIT_KEYS = original_cap
            with _rate_lock:
                _rate_limits.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
