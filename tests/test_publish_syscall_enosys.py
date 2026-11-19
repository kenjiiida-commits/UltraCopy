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
        self.restype = None

    def __call__(self, *args):
        ctypes.set_errno(errno.ENOSYS)
        return -1


class LibcWithoutRenameat2:
    def __init__(self):
        self.syscall = FakeSyscall()


def publish_with_enosys(staged, target, staged_fd, target_fd):
    fake_libc = LibcWithoutRenameat2()

    with patch.object(main.ctypes, "CDLL", return_value=fake_libc):
        main.publish_without_overwrite(
            staged, target, staged_fd, target_fd
        )


def test_regular_file_fallback():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample.txt"
        target = destination / "sample.txt"
        source.write_text("ENOSYS fallback test")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        try:
            publish_with_enosys(source, target, old_fd, new_fd)
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert target.read_text() == "ENOSYS fallback test"
        assert not source.exists()


def test_directory_published_safely():
    """ENOSYS の環境でも、フォルダーは名前の予約方式で上書きせずに公開される。"""
    for existing in (None, "nonempty"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "staging"
            destination = root / "destination"
            staging.mkdir()
            destination.mkdir()

            source = staging / "sample-folder"
            source.mkdir()
            (source / "inside.txt").write_text("original")

            target = destination / "sample-folder"
            if existing:
                target.mkdir()
                (target / "keep.txt").write_text("keep")

            old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
            new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if existing:
                    try:
                        publish_with_enosys(source, target, old_fd, new_fd)
                    except FileExistsError:
                        pass
                    else:
                        raise AssertionError("既存フォルダーがあるのに公開されました")
                else:
                    publish_with_enosys(source, target, old_fd, new_fd)
            finally:
                os.close(old_fd)
                os.close(new_fd)

            if existing:
                assert (source / "inside.txt").read_text() == "original"
                assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
            else:
                assert (target / "inside.txt").read_text() == "original"
                assert not source.exists()


def run_test(name, function):
    try:
        function()
        print(f"[PASS] {name}")
        return True
    except Exception as exc:
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return False


def main_test():
    tests = [
        ("ENOSYS後の通常ファイル・フォールバック公開",
         test_regular_file_fallback),
        ("ENOSYS後のフォルダー公開（予約方式）と既存データ保護",
         test_directory_published_safely),
    ]

    print("=" * 72)
    print("UltraCopy Ver.0.7 ENOSYSフォールバック試験")
    print("=" * 72)

    passed = sum(run_test(name, func) for name, func in tests)

    print("-" * 72)
    print(f"結果: {passed}/{len(tests)} PASS")

    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main_test()
