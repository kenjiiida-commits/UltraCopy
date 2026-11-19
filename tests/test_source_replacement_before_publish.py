from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 最終公開直前のコピー元 inode 置換試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_replacement_before_publish_",
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

    source_file = source / "sample.bin"
    replacement_file = run_dir / "replacement.bin"

    original_content = b"ORIGINAL-BEFORE-PUBLISH-" * 100000
    replacement_content = b"REPLACEMENT-BEFORE-PUBLISH-" * 100000

    source_file.write_bytes(original_content)
    replacement_file.write_bytes(replacement_content)

    original_inode = source_file.stat().st_ino
    replacement_inode = replacement_file.stat().st_ino

    print("\n===== 置換前の状態 =====")
    print(f"元ファイル inode: {original_inode}")
    print(f"置換用ファイル inode: {replacement_inode}")

    if original_inode == replacement_inode:
        print("[FAIL] 2つのファイルが別 inode ではありません。")
        return 1

    original_check = main.CopyWorker._check_source_identity
    check_count = []
    replacement_done = []

    def controlled_check(worker, path):
        if Path(path) == source_file:
            check_count.append(True)

        original_check(worker, path)

        if Path(path) == source_file:
            # 通常コピー経路では、6回目がSHA-256検証後、
            # 7回目が最終公開直前の確認。
            if len(check_count) == 6 and not replacement_done:
                print("\n[試験制御] SHA-256検証後の確認が完了しました。")
                print("[試験操作] 最終公開直前の確認より前にコピー元を置換します。")

                os.replace(replacement_file, source_file)
                replacement_done.append(True)

                current_inode = source_file.stat().st_ino
                print(f"[確認] 置換後 inode: {current_inode}")

                if current_inode != replacement_inode:
                    raise RuntimeError("置換後 inode が一致しません。")

    main.CopyWorker._check_source_identity = controlled_check

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
        main.CopyWorker._check_source_identity = original_check

    target = destination_parent / source.name / source_file.name

    replacement_is_current = (
        source_file.exists()
        and source_file.stat().st_ino == replacement_inode
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )
    partial_files = list(run_dir.rglob("*.partial"))

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 試験結果 =====")

    check("最終公開直前まで inode 確認が進んだ", len(check_count) == 7)
    check("最終確認の直前に置換操作を実行", bool(replacement_done))
    check("コピー元パスが置換後 inode を参照", replacement_is_current)
    check(
        "最終公開直前の inode 確認で置換を検出",
        any(
            "置き換えられました" in event
            for event in failed_events
        )
    )
    check("コピー失敗通知が1回", len(failed_events) == 1)
    check("コピー成功通知なし", len(completed_events) == 0)
    check("最終コピー先を公開していない", not target.exists())
    check("ステージング領域の清掃", len(staging_dirs) == 0)
    check("partialファイルが残っていない", len(partial_files) == 0)

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
