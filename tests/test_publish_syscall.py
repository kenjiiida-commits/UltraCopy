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


# 実際のlibc.syscallを保持し、renameat2関数だけを見えなくする。
real_libc = ctypes.CDLL(None, use_errno=True)


class LibcWithoutRenameat2:
    def __init__(self):
        self.syscall = real_libc.syscall


def publish_via_syscall(staged, target, staged_fd, target_fd):
    fake_libc = LibcWithoutRenameat2()

    with patch.object(main.ctypes, "CDLL", return_value=fake_libc):
        main.publish_without_overwrite(
            staged, target, staged_fd, target_fd
        )


def test_directory_publish():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample-folder"
        source.mkdir()
        (source / "inside.txt").write_text("syscall directory")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        try:
            publish_via_syscall(source, destination / source.name,
                                old_fd, new_fd)
        finally:
            os.close(old_fd)
            os.close(new_fd)

        result = destination / "sample-folder"
        assert (result / "inside.txt").read_text() == "syscall directory"
        assert not source.exists()


def test_existing_directory_not_overwritten():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample-folder"
        target = destination / "sample-folder"

        source.mkdir()
        (source / "source.txt").write_text("source original")
        target.mkdir()
        (target / "target.txt").write_text("destination original")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        try:
            try:
                publish_via_syscall(source, target, old_fd, new_fd)
            except FileExistsError:
                pass
            else:
                raise AssertionError("既存フォルダーが上書きされました")
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert (source / "source.txt").read_text() == "source original"
        assert (target / "target.txt").read_text() == "destination original"


def test_regular_file_publish():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample.txt"
        target = destination / "sample.txt"
        source.write_text("syscall regular file")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        try:
            publish_via_syscall(source, target, old_fd, new_fd)
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert target.read_text() == "syscall regular file"
        assert not source.exists()


def test_symlink_publish():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()

        source = staging / "sample-link"
        target = destination / "sample-link"
        source.symlink_to("relative-target")

        old_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        new_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

        try:
            publish_via_syscall(source, target, old_fd, new_fd)
        finally:
            os.close(old_fd)
            os.close(new_fd)

        assert target.is_symlink()
        assert os.readlink(target) == "relative-target"
        assert not source.is_symlink()


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
        ("syscall経由のフォルダー公開", test_directory_publish),
        ("既存フォルダーの上書き防止", test_existing_directory_not_overwritten),
        ("syscall経由の通常ファイル公開", test_regular_file_publish),
        ("syscall経由のシンボリックリンク公開", test_symlink_publish),
    ]

    print("=" * 68)
    print("UltraCopy Ver.0.7 syscall公開処理テスト")
    print("=" * 68)

    passed = sum(run_test(name, func) for name, func in tests)

    print("-" * 68)
    print(f"結果: {passed}/{len(tests)} PASS")

    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main_test()
