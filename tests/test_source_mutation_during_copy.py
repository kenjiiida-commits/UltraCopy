from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー元変更時の整合性試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_mutation_",
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

    source_file = source / "mutation_test.bin"

    original_content = b"ORIGINAL-CONTENT-" * 65536
    modified_content = b"MODIFIED-CONTENT-" * 65536

    source_file.write_bytes(original_content)

    print("\n===== コピー元の初期状態 =====")
    print(f"ファイルサイズ: {source_file.stat().st_size:,} bytes")
    print(f"元データSHA-256: {main.sha256_file(source_file).hexdigest() if False else '検証時に取得'}")

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
    mutation_done = []

    def copy_then_modify(src_path, dst_path):
        original_copy_stream(src_path, dst_path)

        if Path(src_path) == source_file and not mutation_done:
            source_file.write_bytes(modified_content)
            mutation_done.append(True)
            print("\n[試験操作] コピー直後にコピー元ファイルを書き換えました。")

    worker._copy_stream = copy_then_modify

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

    print("\n===== 整合性検証結果 =====")

    check(
        "コピー処理中にコピー元の変更操作が実行された",
        mutation_done
    )

    check(
        "コピー元ファイルが変更後の内容になっている",
        source_file.read_bytes() == modified_content
    )

    check(
        "コピー元と一時コピーのSHA-256不一致を検出した",
        any("SHA-256検証不一致" in line for line in logs)
        or any("SHA-256検証不一致" in event for event in failed_events)
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

    check(
        "コピー元ファイルの変更後の内容が保持されている",
        source_file.read_bytes() == modified_content
    )

    if failed_events:
        print("\n===== エラー通知内容 =====")
        print(failed_events[0])

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "SHA-256",
            "エラー",
            "清掃",
        )):
            print(line)

    passed = sum(checks)
    total = len(checks)

    print("\n" + "=" * 72)
    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main_test())
