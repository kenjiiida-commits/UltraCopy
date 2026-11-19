from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー途中の元ファイル変更試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_mutation_mid_copy_",
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

    source_file = source / "mid_copy_test.bin"

    original_content = b"ORIGINAL-MID-COPY-DATA-" * 500000
    modified_prefix = b"MODIFIED-DURING-COPY-2026\n"
    modified_prefix = (
        modified_prefix * ((4096 // len(modified_prefix)) + 1)
    )[:4096]

    source_file.write_bytes(original_content)

    first_chunk_size = []
    mutation_done = []

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

    original_copy_file_range = os.copy_file_range

    def controlled_copy_file_range(src_fd, dst_fd, count):
        copied = original_copy_file_range(src_fd, dst_fd, count)

        if copied > 0 and not mutation_done:
            first_chunk_size.append(copied)

            print(
                f"\n[試験制御] 最初のデータ転送を確認: "
                f"{copied:,} bytes"
            )
            print(
                "[試験操作] 次のデータ転送に進む前に"
                "コピー元ファイルを変更します。"
            )

            with source_file.open("r+b", buffering=0) as stream:
                stream.seek(0)
                written = stream.write(modified_prefix)

                if written != len(modified_prefix):
                    raise RuntimeError("元ファイルの変更が不完全です。")

                os.fsync(stream.fileno())

            mutation_done.append(True)

            if source_file.read_bytes()[:4096] != modified_prefix:
                raise RuntimeError("元ファイルの変更を確認できません。")

            print("[確認] コピー元の先頭4KiBが変更されました。")

        return copied

    os.copy_file_range = controlled_copy_file_range

    try:
        print("\n===== コピー処理開始 =====")
        worker.run()
    finally:
        os.copy_file_range = original_copy_file_range

    target = destination_parent / source.name

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== コピー途中の変更・整合性検証 =====")

    check(
        "最初のデータ転送が実行された",
        bool(first_chunk_size)
    )

    check(
        "最初の転送量がファイル全体より小さい",
        bool(first_chunk_size)
        and first_chunk_size[0] < len(original_content)
    )

    check(
        "コピー途中に元ファイルの変更操作が実行された",
        bool(mutation_done)
    )

    check(
        "コピー元ファイルに変更後の内容が保持されている",
        source_file.read_bytes()[:4096] == modified_prefix
    )

    check(
        "SHA-256検証不一致を検出した",
        any(
            "SHA-256検証不一致" in event
            for event in failed_events
        )
        or any(
            "SHA-256検証不一致" in line
            for line in logs
        )
    )

    check(
        "失敗通知が1回だけ発生した",
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

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "一時コピー領域が削除されている",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "一時ファイル（.partial）が残っていない",
        len(partial_files) == 0
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
