from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 SHA-256計算直後のコピー元変更試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_mutation_after_hash_",
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

    source_file = source / "after_hash_test.bin"

    original_content = b"ORIGINAL-BEFORE-HASH-" * 500000
    modified_prefix = (
        b"MODIFIED-AFTER-HASH-2026\n" * 200
    )[:4096]

    source_file.write_bytes(original_content)

    original_sha256_file = main.sha256_file
    source_hash_completed = []

    def controlled_sha256_file(path, checkpoint):
        digest = original_sha256_file(path, checkpoint)

        if Path(path) == source_file and not source_hash_completed:
            print("\n[試験制御] コピー元のSHA-256計算が完了しました。")
            print("[試験操作] 一時コピーのSHA-256計算前にコピー元を変更します。")

            with source_file.open("r+b", buffering=0) as stream:
                stream.seek(0)
                written = stream.write(modified_prefix)

                if written != len(modified_prefix):
                    raise RuntimeError("コピー元の変更が不完全です。")

                stream.flush()

            source_hash_completed.append(True)

            if source_file.read_bytes()[:4096] != modified_prefix:
                raise RuntimeError("コピー元の変更を確認できません。")

            print("[確認] コピー元の変更が完了しました。")

        return digest

    main.sha256_file = controlled_sha256_file

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

    try:
        print("\n===== CopyWorker実行 =====")
        worker.run()
    finally:
        main.sha256_file = original_sha256_file

    target = destination_parent / source.name / source_file.name

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 検証直後の変更結果 =====")

    check(
        "コピー元のSHA-256計算後に変更操作が実行された",
        bool(source_hash_completed)
    )

    check(
        "コピー元が変更後の内容になっている",
        source_file.read_bytes()[:4096] == modified_prefix
    )

    check(
        "SHA-256再検証で変更を検出した",
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

    if completed_events:
        print("\n===== 成功通知 =====")
        print(completed_events[0])

    if failed_events:
        print("\n===== 失敗通知 =====")
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
    raise SystemExit(main_test())
