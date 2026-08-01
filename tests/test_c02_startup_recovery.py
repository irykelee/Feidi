"""Regression tests for C-02 — startup cleanup vs in-flight recovery.

The pre-fix startup order ran ``_startup_cleanup`` (which deleted every
``feidi_chunks/<tid>/`` directory) BEFORE ``_load_chunk_states`` (which
re-populated the in-memory ``chunk_transfers`` from each ``.state.json``),
so the recovered state claimed chunks that no longer existed on disk.
The atexit ``cleanup()`` path added a second silent deletion: ``force=True``
unconditionally ``shutil.rmtree``'d every transfer directory.

These tests run against a real ``transfer.py`` subprocess that imports the
module (which causes main() to execute start-up cleanup + state load +
atexit registration). Each test seeds ``feidi_chunks/`` with a controlled
layout, runs the server briefly, and asserts the surviving disk state.

Run with:
    python3 -m unittest tests.test_c02_startup_recovery -v
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHUNK_DIR = os.path.join(ROOT, "feidi_chunks")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server_once(port: int, hold_seconds: float = 2.2) -> None:
    """Spawn the real server, let main() complete startup, then SIGTERM.
    SIGTERM hits ``signal_handler`` → ``cleanup()`` → ``sys.exit(0)`` →
    atexit, all paths that existed pre-fix and remain in play post-fix."""
    proc = subprocess.Popen(
        [sys.executable, "transfer.py",
         "--port", str(port), "--bind", "127.0.0.1", "--no-browser"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(hold_seconds)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def touch_state(tid: str, total: int, chunks: list[int], extra: dict | None = None) -> str:
    state = {
        "chunks": chunks,
        "total": total,
        "bytes_received": 0,
        "info": {"name": "sm.bin", "size": 1024, "mime": "application/octet-stream"},
        "sender": "pc",
        "device_name": "Tester",
        "device_id": "deadbeef",
        "target_id": None,
        "is_image": False,
        "last_activity": time.time(),
    }
    if extra:
        state.update(extra)
    path = os.path.join(CHUNK_DIR, tid + ".state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


def write_chunks(tid: str, indexes: list[int], body: bytes = b"\x42") -> list[str]:
    tdir = os.path.join(CHUNK_DIR, tid)
    os.makedirs(tdir, exist_ok=True)
    paths = []
    for i in indexes:
        p = os.path.join(tdir, f"{i}.chunk")
        with open(p, "wb") as f:
            f.write(body)
        paths.append(p)
    return paths


class FeidiChunkDirTestCase(unittest.TestCase):
    """Common scaffold: makes each test get a fresh, clean CHUNK_DIR and
    restarts the on-disk state to whatever it was before setUp ran."""

    SNAP = CHUNK_DIR + ".__test_snapshot__"

    def setUp(self):
        # 1. Take a snapshot of the existing CHUNK_DIR contents (if any).
        if os.path.isdir(self.SNAP):
            shutil.rmtree(self.SNAP, ignore_errors=True)
        if os.path.isdir(CHUNK_DIR):
            shutil.copytree(CHUNK_DIR, self.SNAP)
            shutil.rmtree(CHUNK_DIR)
        os.makedirs(CHUNK_DIR, exist_ok=True)
        # Lists used to clean up just the seed files we created in the test
        # body, before restoring the snapshot.
        self._seed_dirs = []
        self._seed_state_files = []

    def tearDown(self):
        # Remove only the test's own seeds; snapshot will be restored below
        # by the registration in setUp.
        for f in self._seed_state_files:
            try: os.remove(f)
            except OSError: pass
        for d in self._seed_dirs:
            shutil.rmtree(d, ignore_errors=True)
        # Restore CHUNK_DIR to its pre-test contents.
        if os.path.isdir(CHUNK_DIR):
            shutil.rmtree(CHUNK_DIR, ignore_errors=True)
        if os.path.isdir(self.SNAP):
            shutil.move(self.SNAP, CHUNK_DIR)


class ValidStateSurvivesStartup(FeidiChunkDirTestCase):
    """C-02 happy path: state file + all declared chunk files present →
    after startup, both should still be on disk so a later POST can
    complete the transfer."""

    def test_chunk_files_kept_when_state_valid(self):
        tid = "ok_tid_abcd1234"
        self._seed_dirs.append(os.path.join(CHUNK_DIR, tid))
        write_chunks(tid, [0, 1])
        self._seed_state_files.append(touch_state(tid, total=2, chunks=[0, 1]))

        run_server_once(free_port())

        self.assertTrue(
            os.path.isfile(os.path.join(CHUNK_DIR, tid, "0.chunk")),
            "chunk 0 should survive startup alongside its state",
        )
        self.assertTrue(
            os.path.isfile(os.path.join(CHUNK_DIR, tid, "1.chunk")),
            "chunk 1 should survive startup alongside its state",
        )
        self.assertTrue(
            os.path.isfile(os.path.join(CHUNK_DIR, tid + ".state.json")),
            "state file should survive startup",
        )


class InconsistentStateDropped(FeidiChunkDirTestCase):
    """C-02 core symptom: ``.state.json`` says chunks {0,1} but the
    directory only has ``0.chunk``. The whole entry (state + dir) is
    unrecoverable and must be removed cleanly instead of left in a
    half-truth state."""

    def test_partial_chunks_purge_state_and_dir(self):
        tid = "broken_tid_5678"
        self._seed_dirs.append(os.path.join(CHUNK_DIR, tid))
        # write only chunk 0; state claims [0, 1]
        write_chunks(tid, [0])
        state_path = touch_state(tid, total=2, chunks=[0, 1])
        self._seed_state_files.append(state_path)

        run_server_once(free_port())

        self.assertFalse(
            os.path.isdir(os.path.join(CHUNK_DIR, tid)),
            "inconsistent transfer dir should be deleted at startup",
        )
        self.assertFalse(
            os.path.isfile(state_path),
            "state for unrecoverable transfer should be deleted",
        )


class OrphanDirectoryCleaned(FeidiChunkDirTestCase):
    """A transfer directory with NO matching ``.state.json`` is an
    orphan from a prior aborted run; startup cleanup should remove it."""

    def test_orphan_dir_without_state_is_removed(self):
        tid = "orphan_tid_9999"
        self._seed_dirs.append(os.path.join(CHUNK_DIR, tid))
        write_chunks(tid, [0, 1, 2])
        # no state.json

        run_server_once(free_port())

        self.assertFalse(
            os.path.isdir(os.path.join(CHUNK_DIR, tid)),
            "orphan transfer dir (no state) should be removed by startup",
        )


class StateOnlyWithNoDir(FeidiChunkDirTestCase):
    """A ``.state.json`` whose transfer directory never existed (or was
    deleted by user / crash mid-shutdown) must also be pruned — otherwise
    the next chunk POST referencing its ``transfer_id`` would silently
    re-create the entry and crash at assembly."""

    def test_state_only_purged_when_no_chunks(self):
        tid = "phantom_tid_7777"
        state_path = touch_state(tid, total=2, chunks=[0, 1])
        self._seed_state_files.append(state_path)
        # no transfer_dir at all

        run_server_once(free_port())

        self.assertFalse(
            os.path.isfile(state_path),
            "state file referring to a missing transfer dir should be deleted",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
