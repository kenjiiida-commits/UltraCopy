from pathlib import Path
import ctypes
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


# renameat2関数だけを見えなくし、実際のsyscallを使用する。
real_libc = ctypes.CDLL(None, use_errno=True)


class LibcWithoutRenameat2:
    def __init__(self):
        self.syscall = real_libc.syscall


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 syscallフォルダーコピー統合テスト")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="publish_syscall_integration_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source-folder"
    destination_parent = run_dir / "destination"
    source.mkdir()
    destination_parent.mkdir()

    (source / "sample.txt").write_text(
        "UltraCopy syscall integration test\n"
        "日本語のフォルダーコピー検証です。\n",
        encoding="utf-8"
    )

    nested = source / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text(
        "nested directory content\n",
        encoding="utf-8"
    )

    expected_files = {
        "sample.txt": (source / "sample.txt").read_bytes(),
        "nested/inside.txt": (nested / "inside.txt").read_bytes(),
    }

    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    fake_libc = LibcWithoutRenameat2()

    with patch.object(main.ctypes, "CDLL", return_value=fake_libc):
        worker.run()

    copied_folder = destination_parent / source.name

    checks = [
        (
            "syscall経由でフォルダーコピー完了",
            len(completed) == 1 and len(failed) == 0
        ),
        (
            "コピー先フォルダーが存在",
            copied_folder.is_dir()
        ),
        (
            "直下ファイルの内容一致",
            (copied_folder / "sample.txt").is_file()
            and (copied_folder / "sample.txt").read_bytes()
            == expected_files["sample.txt"]
        ),
        (
            "サブフォルダー内ファイルの内容一致",
            (copied_folder / "nested" / "inside.txt").is_file()
            and (copied_folder / "nested" / "inside.txt").read_bytes()
            == expected_files["nested/inside.txt"]
        ),
        (
            "SHA-256検証済み",
            worker._verification_result == "SHA-256検証済み"
        ),
        (
            "ステージング領域が清掃済み",
            not list(destination_parent.glob(".ultracopy-staging-*"))
        ),
    ]

    print()
    print("===== 検証結果 =====")

    passed = 0

    for description, condition in checks:
        if condition:
            print(f"[PASS] {description}")
            passed += 1
        else:
            print(f"[FAIL] {description}")

    print()
    print("===== シグナル確認 =====")
    print(f"完了通知: {len(completed)} 件")
    print(f"失敗通知: {len(failed)} 件")

    if failed:
        for message in failed:
            print(message)

    print()
    print("===== 清掃ログ =====")

    for message in logs:
        if "清掃" in message:
            print(message)

    print("-" * 72)
    print(f"結果: {passed}/{len(checks)} PASS")

    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main_test()
