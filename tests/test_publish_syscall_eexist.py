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


class FakeSyscall:
    def __init__(self):
        self.called = False
        self.restype = None

    def __call__(self, *args):
        self.called = True
        ctypes.set_errno(errno.EEXIST)
        return -1


class FakeLibc:
    def __init__(self):
        self.syscall = FakeSyscall()


def test_syscall_eexist_does_not_fallback():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample.txt"
        target = destination / "sample.txt"

        source.write_text("source original")
        target.write_text("destination original")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        fake_libc = FakeLibc()

        try:
            with patch.object(
                main.ctypes, "CDLL", return_value=fake_libc
            ):
                try:
                    main.publish_without_overwrite(
                        source, target, old_fd, new_fd
                    )
                except FileExistsError:
                    pass
                else:
                    raise AssertionError(
                        "syscallのEEXISTがFileExistsErrorになりませんでした"
                    )
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert fake_libc.syscall.called
        assert source.read_text() == "source original"
        assert target.read_text() == "destination original"


if __name__ == "__main__":
    print("=" * 72)
    print("UltraCopy Ver.0.7 syscall EEXISTエラー処理テスト")
    print("=" * 72)

    test_syscall_eexist_does_not_fallback()

    print("[PASS] syscallのEEXIST時に従来方式へ移行しない")
    print("[PASS] コピー元と既存コピー先の内容を保持")
    print("-" * 72)
    print("結果: 2/2 PASS")
