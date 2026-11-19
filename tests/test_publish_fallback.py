from pathlib import Path
import errno
import os
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def run_test(name, test_function):
    try:
        test_function()
        print(f"[PASS] {name}")
        return True
    except Exception as exc:
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return False


def with_fallback(staged, target, staged_fd, target_fd):
    # renameat2 が存在しない環境を再現する。
    with patch.object(main.ctypes, "CDLL", return_value=object()):
        main.publish_without_overwrite(
            staged, target, staged_fd, target_fd
        )


def test_regular_file():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staged_dir = root / "staging"
        target_dir = root / "destination"
        staged_dir.mkdir()
        target_dir.mkdir()

        staged = staged_dir / "sample.txt"
        target = target_dir / "sample.txt"
        staged.write_text("fallback file", encoding="utf-8")

        staged_fd = os.open(staged_dir, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with_fallback(staged, target, staged_fd, target_fd)
        finally:
            os.close(staged_fd)
            os.close(target_fd)

        assert target.read_text(encoding="utf-8") == "fallback file"
        assert not staged.exists()


def test_symlink():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staged_dir = root / "staging"
        target_dir = root / "destination"
        staged_dir.mkdir()
        target_dir.mkdir()

        staged = staged_dir / "sample-link"
        target = target_dir / "sample-link"
        staged.symlink_to("relative-target")

        staged_fd = os.open(staged_dir, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with_fallback(staged, target, staged_fd, target_fd)
        finally:
            os.close(staged_fd)
            os.close(target_fd)

        assert target.is_symlink()
        assert os.readlink(target) == "relative-target"
        assert not staged.is_symlink()


def test_existing_target():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staged_dir = root / "staging"
        target_dir = root / "destination"
        staged_dir.mkdir()
        target_dir.mkdir()

        staged = staged_dir / "sample.txt"
        target = target_dir / "sample.txt"
        staged.write_text("new content", encoding="utf-8")
        target.write_text("original content", encoding="utf-8")

        staged_fd = os.open(staged_dir, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                with_fallback(staged, target, staged_fd, target_fd)
            except FileExistsError:
                pass
            else:
                raise AssertionError("既存ファイルがあるのに公開されました")
        finally:
            os.close(staged_fd)
            os.close(target_fd)

        assert target.read_text(encoding="utf-8") == "original content"
        assert staged.read_text(encoding="utf-8") == "new content"


def _directory_case(existing=None):
    """renameat2 が使えない環境でのフォルダー公開。existing: 公開先に先にある項目。"""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staged_dir = root / "staging"
        target_dir = root / "destination"
        staged_dir.mkdir()
        target_dir.mkdir()

        staged = staged_dir / "sample-folder"
        target = target_dir / "sample-folder"
        staged.mkdir()
        (staged / "inside.txt").write_text("data", encoding="utf-8")
        if existing == "nonempty":
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")
        elif existing == "empty":
            target.mkdir()
        elif existing == "file":
            target.write_text("keep", encoding="utf-8")

        staged_fd = os.open(staged_dir, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if existing is None:
                with_fallback(staged, target, staged_fd, target_fd)
            else:
                try:
                    with_fallback(staged, target, staged_fd, target_fd)
                except FileExistsError:
                    pass
                else:
                    raise AssertionError("既存の項目があるのに公開されました")
        finally:
            os.close(staged_fd)
            os.close(target_fd)

        if existing is None:
            # 名前を予約する方式で、上書きせずに公開される（Ver.0.7.7 以降）。
            assert (target / "inside.txt").read_text(encoding="utf-8") == "data"
            assert not staged.exists()
        else:
            # 既存の項目は一切変更されず、コピー側もそのまま残る。
            assert (staged / "inside.txt").read_text(encoding="utf-8") == "data"
            if existing == "nonempty":
                assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
                assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
            elif existing == "empty":
                assert target.is_dir() and not any(target.iterdir())
            else:
                assert target.read_text(encoding="utf-8") == "keep"


def test_directory_published_by_reservation():
    _directory_case()


def test_directory_existing_not_overwritten():
    for existing in ("nonempty", "empty", "file"):
        _directory_case(existing)


def main_test():
    print("=" * 68)
    print("UltraCopy Ver.0.7 フォールバック公開処理テスト")
    print("=" * 68)

    tests = [
        ("通常ファイルの公開", test_regular_file),
        ("シンボリックリンクの公開", test_symlink),
        ("既存ファイルの上書き防止", test_existing_target),
        ("フォルダーを名前の予約方式で公開", test_directory_published_by_reservation),
        ("既存のフォルダー・ファイルを上書きしない（予約方式）",
         test_directory_existing_not_overwritten),
    ]

    passed = sum(run_test(name, func) for name, func in tests)
    print("-" * 68)
    print(f"結果: {passed}/{len(tests)} PASS")

    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main_test()
