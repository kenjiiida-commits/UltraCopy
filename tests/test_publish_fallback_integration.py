from pathlib import Path
import os
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 フォールバック統合テスト")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="publish_fallback_integration_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "sample.txt"
    destination_parent = run_dir / "destination"

    destination_parent.mkdir()

    source_file = source
    source_file.write_text(
        "UltraCopy fallback integration test\n"
        "日本語の内容も確認します。\n",
        encoding="utf-8"
    )

    expected_content = source_file.read_bytes()

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

    # renameat2 が存在しない環境を再現してCopyWorker全体を実行。
    with patch.object(main.ctypes, "CDLL", return_value=object()):
        worker.run()

    copied_file = destination_parent / source.name

    checks = [
        (
            "フォールバック経由でコピー完了",
            len(completed) == 1 and len(failed) == 0
        ),
        (
            "コピー先ファイルが存在",
            copied_file.is_file()
        ),
        (
            "コピー内容が完全一致",
            copied_file.is_file()
            and copied_file.read_bytes() == expected_content
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
