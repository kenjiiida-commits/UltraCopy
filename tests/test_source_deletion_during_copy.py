from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー元削除時の整合性試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_deletion_",
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

    source_file = source / "deletion_test.bin"
    original_content = b"DELETION-TEST-DATA-" * 65536
    source_file.write_bytes(original_content)

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

    original_copy_stream = worker._copy_stream
    deletion_done = []

    def copy_then_delete(src_path, dst_path):
        original_copy_stream(src_path, dst_path)

        if Path(src_path) == source_file and not deletion_done:
            source_file.unlink()
            deletion_done.append(True)
            print("\n[試験操作] コピー直後にコピー元ファイルを削除しました。")

    worker._copy_stream = copy_then_delete

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1

    target = destination_parent / source.name

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 削除時の整合性検証 =====")

    check(
        "コピー元ファイルの削除操作が実行された",
        deletion_done
    )

    check(
        "コピー元ファイルが実際に削除されている",
        not source_file.exists()
    )

    check(
        "SHA-256検証時にコピー元の消失を検出した",
        any(
            "FileNotFoundError" in event
            or "No such file or directory" in event
            or "コピー元ファイルを確認できません" in event
            for event in failed_events
        )
    )

    check(
        "失敗通知が発生した",
        len(failed_events) == 1
    )

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "最終コピー先が公開されていない",
        not target.exists()
    )

    staging_dirs = list(destination_parent.glob(".ultracopy-staging-*"))

    check(
        "一時コピー領域が削除されている",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "一時ファイル（.partial）が残っていない",
        len(partial_files) == 0
    )

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "SHA-256",
            "エラー",
            "清掃",
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
