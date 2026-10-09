"""Real POSIX exclusion and simulated Windows filesystem behavior."""
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from video_notes import calls, locking, usage
from video_notes.delivery import feishu
from video_notes.run import run_lock


class Portability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def windows(self, callback):
        module = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=callback)
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"msvcrt": module}).start()
        patch.object(locking, "_WINDOWS", True).start()
        return module

    @unittest.skipIf(os.name == "nt", "Actual POSIX process-lock test")
    def test_run_lock_excludes_a_separate_process_and_releases(self):
        code = (
            "import sys\nfrom video_notes.run import run_lock\n"
            "with run_lock(sys.argv[1]):\n"
            " print('locked', flush=True)\n sys.stdin.readline()\n"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "Child did not acquire its lock")
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(RuntimeError, "Another command owns"):
                with run_lock(self.root):
                    self.fail("Conflicting process acquired the run")
            _, error = process.communicate("release\n", timeout=10)
            self.assertEqual(process.returncode, 0, error)
            with run_lock(self.root):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_run_lock_preserves_a_body_io_failure_and_releases(self):
        with self.assertRaisesRegex(BlockingIOError, "body failure"):
            with run_lock(self.root):
                raise BlockingIOError("body failure")
        with run_lock(self.root):
            pass

    def test_windows_lock_releases_even_when_body_raises(self):
        events = []

        def callback(descriptor, mode, count):
            events.append((mode, count, os.lseek(descriptor, 0, os.SEEK_CUR)))

        module = self.windows(callback)
        path = self.root / ".lock"
        with self.assertRaisesRegex(ValueError, "body failure"):
            with locking.file_lock(path):
                self.assertEqual(path.read_bytes(), b"\0")
                raise ValueError("body failure")
        self.assertEqual(events, [(module.LK_NBLCK, 1, 0), (module.LK_UNLCK, 1, 0)])

    def test_windows_blocking_lock_waits_on_contention_only(self):
        events = []

        def callback(descriptor, mode, count):
            events.append(mode)
            if len(events) == 1:
                raise OSError(errno.EACCES, "held")

        module = self.windows(callback)
        with patch.object(locking.time, "sleep") as sleep:
            with locking.file_lock(self.root / ".lock"):
                pass
        self.assertEqual(events, [module.LK_NBLCK, module.LK_NBLCK, module.LK_UNLCK])
        sleep.assert_called_once_with(0.05)

    def test_windows_nonblocking_conflict_does_not_unlock_or_wait(self):
        events = []

        def callback(descriptor, mode, count):
            events.append(mode)
            raise OSError(errno.EACCES, "held")

        module = self.windows(callback)
        with patch.object(locking.time, "sleep") as sleep:
            with self.assertRaises(BlockingIOError):
                with locking.file_lock(self.root / ".lock", blocking=False):
                    self.fail("Acquired a held lock")
        self.assertEqual(events, [module.LK_NBLCK])
        sleep.assert_not_called()

    def test_windows_first_byte_initialization_conflict_uses_lock_wait_policy(self):
        events = []
        module = self.windows(lambda descriptor, mode, count: events.append(mode))
        stream = types.SimpleNamespace(fileno=lambda: 123, seek=lambda offset: None)
        from unittest.mock import Mock
        stream.write = Mock(side_effect=[OSError(errno.EACCES, "held byte"), 1])
        with patch.object(os, "fstat", return_value=types.SimpleNamespace(st_size=0)), \
                patch.object(locking.time, "sleep") as sleep:
            locking._acquire(stream, True)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(events, [module.LK_NBLCK])
        stream.write.assert_any_call(b"\0")

        events.clear()
        stream.write = Mock(side_effect=OSError(errno.EACCES, "held byte"))
        with patch.object(os, "fstat", return_value=types.SimpleNamespace(st_size=0)), \
                patch.object(locking.time, "sleep") as sleep:
            with self.assertRaises(BlockingIOError):
                locking._acquire(stream, False)
        sleep.assert_not_called()
        self.assertEqual(events, [])

    def test_windows_unexpected_lock_failure_propagates(self):
        self.windows(lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "storage failure")))
        with patch.object(locking.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "storage failure"):
                with locking.file_lock(self.root / ".lock"):
                    pass
        sleep.assert_not_called()

    def test_windows_ledger_uses_sidecar_and_retains_jsonl_deduplication(self):
        events = []
        self.windows(lambda descriptor, mode, count: events.append(mode))
        path = self.root / "usage.jsonl"
        usage.ledger_append(path, {"run_id": "one", "cost": None})
        usage.ledger_append(path, {"run_id": "one", "cost": None})
        usage.ledger_append(path, {"run_id": "two", "cost": None})
        self.assertEqual([json.loads(line)["run_id"] for line in path.read_text().splitlines()], ["one", "two"])
        self.assertNotIn(b"\0", path.read_bytes())
        self.assertEqual(path.with_name("usage.jsonl.lock").read_bytes(), b"\0")
        self.assertEqual(len(events), 6)

    def test_windows_receipts_fsync_files_without_opening_directory(self):
        self.windows(lambda *args: None)
        fsync = os.fsync
        for writer, name in ((calls.durable_save, "attempt.json"), (feishu._save, "delivery.json")):
            with self.subTest(writer=name), patch.object(os, "fsync", wraps=fsync) as flush:
                writer(self.root / name, {"status": "inflight"})
                self.assertEqual(json.loads((self.root / name).read_text()), {"status": "inflight"})
                self.assertEqual(flush.call_count, 1)

    def test_directory_sync_reports_unsupported_and_propagates_storage_failure(self):
        with patch.object(locking, "_WINDOWS", False), patch.object(os, "open", return_value=123), \
                patch.object(os, "close") as close:
            with patch.object(os, "fsync", side_effect=OSError(errno.EINVAL, "unsupported")):
                self.assertFalse(locking.sync_directory(self.root))
            close.assert_called_once_with(123)
            with patch.object(os, "fsync", side_effect=OSError(errno.EIO, "storage failure")):
                with self.assertRaisesRegex(OSError, "storage failure"):
                    locking.sync_directory(self.root)

    def test_modules_import_without_posix_fcntl(self):
        code = (
            "import builtins\noriginal = builtins.__import__\n"
            "def guarded(name, *args, **kwargs):\n"
            " if name == 'fcntl': raise ImportError('No POSIX fcntl')\n"
            " return original(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "from video_notes import run, usage, notes, calls, engine\n"
            "from video_notes.delivery import feishu\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
