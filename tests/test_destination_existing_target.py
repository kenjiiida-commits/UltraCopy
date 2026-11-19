from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先既存ファイル上書き防止試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_existing_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    test_data = run_dir / "test_data"
    test_data.mkdir()

    main.DATA_DIR = test_data
    main.DB_PATH = test_data / "history.sqlite3"

    source = run_dir / "source_data"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    source_file = source / "important.txt"
    source_content = b"NEW SOURCE CONTENT\n"
    source_file.write_bytes(source_content)

    target = destination_parent / source.name / source_file.name
    target.parent.mkdir()

    existing_content = b"EXISTING DESTINATION CONTENT - DO NOT OVERWRITE\n"
    target.write_bytes(existing_content)

    original_stat = target.stat()
    original_mtime_ns = original_stat.st_mtime_ns
    original_size = original_stat.st_size
    original_inode = original_stat.st_ino

    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed_events = []
    failed_events = []
    logs = []

    worker.completed.connect(completed_events.append)
    worker.failed.connect(failed_events.append)
    worker.log.connect(logs.append)

    print("\n===== CopyWorker実行 =====")
    print(f"コピー元: {source}")
    print(f"既存コピー先: {target}")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 上書き防止の検証 =====")

    check(
        "既存コピー先ファイルが存在している",
        target.is_file()
    )

    check(
        "既存ファイルの内容が完全に保持されている",
        target.exists() and target.read_bytes() == existing_content
    )

    check(
        "既存ファイルのサイズが変化していない",
        target.exists() and target.stat().st_size == original_size
    )

    check(
        "既存ファイルの更新日時が変化していない",
        target.exists() and target.stat().st_mtime_ns == original_mtime_ns
    )

    check(
        "既存ファイルのinodeが変化していない",
        target.exists() and target.stat().st_ino == original_inode
    )

    check(
        "コピー失敗通知が1回発生した",
        len(failed_events) == 1
    )

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "同名項目が存在する旨のエラーが通知された",
        len(failed_events) == 1
        and "同名の項目があります" in failed_events[0]
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "ステージング領域が残っていない",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "一時ファイルが残っていない",
        len(partial_files) == 0
    )

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "エラー",
            "清掃",
            "同名",
        )):
            print(line)

    if failed_events:
        print("\n===== エラー通知内容 =====")
        print(failed_events[0])

    passed = sum(checks)
    total = len(checks)

    print("\n" + "=" * 72)
    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main_test())
