from pathlib import Path
import ctypes
import errno
import os
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main

# syscallは実際のlibcのものを使用する。
real_libc = ctypes.CDLL(None, use_errno=True)


class FakeRenameat2:
    def __init__(self):
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        ctypes.set_errno(errno.ENOSYS)
        return -1


class FakeLibc:
    def __init__(self):
        # renameat2関数は存在するが、ENOSYSを返す。
        self.renameat2 = FakeRenameat2()

        # syscall経由では実際のrenameat2を実行する。
        self.syscall = real_libc.syscall


def test_regular_file_uses_syscall_after_renameat2_enosys():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample.txt"
        target = destination / "sample.txt"
        source.write_text("renameat2 ENOSYS recovery test")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        fake_libc = FakeLibc()

        try:
            with patch.object(
                main.ctypes, "CDLL", return_value=fake_libc
            ):
                main.publish_without_overwrite(
                    source, target, old_fd, new_fd
                )
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert target.read_text() == "renameat2 ENOSYS recovery test"
        assert not source.exists()


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 renameat2 ENOSYS回復テスト")
    print("=" * 72)

    try:
        test_regular_file_uses_syscall_after_renameat2_enosys()
        print("[PASS] renameat2のENOSYS後にsyscall経由で公開")
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1)

    print("-" * 72)
    print("結果: 1/1 PASS")


if __name__ == "__main__":
    main_test()
