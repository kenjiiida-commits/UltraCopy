from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 属性コピー中のコピー元 inode 置換試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_replacement_during_copystat_",
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

    original_content = b"ORIGINAL-COPY-STAT-" * 100000
    replacement_content = b"REPLACEMENT-COPY-STAT-" * 100000

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

    original_copystat = main.shutil.copystat
    replacement_done = []

    def replace_during_copystat(src_path, dst_path, *args, **kwargs):
        if Path(src_path) == source_file and not replacement_done:
            print("\n[試験制御] 属性コピー関数が呼び出されました。")
            print("[試験操作] 属性コピーの実行中にコピー元を置換します。")

            os.replace(replacement_file, source_file)
            replacement_done.append(True)

            current_inode = source_file.stat().st_ino
            print(f"[確認] 置換後 inode: {current_inode}")

            if current_inode != replacement_inode:
                raise RuntimeError("置換後 inode が一致しません。")

        return original_copystat(
            src_path, dst_path, *args, **kwargs
        )

    main.shutil.copystat = replace_during_copystat

    try:
        print("\n===== CopyWorker実行 =====")
        worker.run()
    finally:
        main.shutil.copystat = original_copystat

    target = destination_parent / source.name
    target_file = target / source_file.name

    replacement_is_current = (
        source_file.exists()
        and source_file.stat().st_ino == replacement_inode
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    print("\n===== 試験結果 =====")
    print(f"[{'PASS' if replacement_done else 'FAIL'}] 属性コピー中の置換操作")
    print(f"[{'PASS' if replacement_is_current else 'FAIL'}] コピー元パスが置換後 inode を参照")
    print(f"[{'PASS' if len(failed_events) == 1 else 'FAIL'}] コピー失敗通知が1回")
    print(f"[{'PASS' if not completed_events else 'FAIL'}] コピー成功通知なし")
    print(f"[{'PASS' if not target_file.exists() else 'FAIL'}] コピー先ファイルを公開していない")
    print(f"[{'PASS' if not staging_dirs else 'FAIL'}] ステージング領域の清掃")

    print("\n===== 失敗通知 =====")
    for event in failed_events:
        print(event)

    print("\n===== 関連ログ =====")
    for line in logs:
        print(line)

    passed = (
        bool(replacement_done)
        and replacement_is_current
        and len(failed_events) == 1
        and not completed_events
        and not target_file.exists()
        and not staging_dirs
    )

    print("\n" + "=" * 72)
    print(f"判定: {'PASS' if passed else 'FAIL'}")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
