from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー元ファイル置換・読み取り開始前の診断試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="source_replacement_before_open_",
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

    original_content = b"ORIGINAL-INODE-CONTENT-" * 100000
    replacement_content = b"REPLACEMENT-INODE-CONTENT-" * 100000

    source_file.write_bytes(original_content)
    replacement_file.write_bytes(replacement_content)

    original_inode = source_file.stat().st_ino
    replacement_inode = replacement_file.stat().st_ino

    print("\n===== 置換前の状態 =====")
    print(f"元ファイル inode: {original_inode}")
    print(f"置換用ファイル inode: {replacement_inode}")
    print(f"元ファイルサイズ: {source_file.stat().st_size:,} bytes")
    print(f"置換用サイズ: {replacement_file.stat().st_size:,} bytes")

    if original_inode == replacement_inode:
        print("[ERROR] 置換前の2ファイルが別 inode ではありません。")
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

    original_copy_stream = worker._copy_stream
    replacement_done = []

    def replace_before_open(src_path, temp_path):
        if Path(src_path) == source_file and not replacement_done:
            print("\n[試験制御] コピー元の属性取得後、読み取り開始直前です。")
            print("[試験操作] コピー元パスを別 inode のファイルに置換します。")

            os.replace(replacement_file, source_file)
            replacement_done.append(True)

            current_inode = source_file.stat().st_ino
            print(f"[確認] 置換後 inode: {current_inode}")

            if current_inode != replacement_inode:
                raise RuntimeError("コピー元パスの inode 置換を確認できません。")

        return original_copy_stream(src_path, temp_path)

    worker._copy_stream = replace_before_open

    print("\n===== CopyWorker実行 =====")
    worker.run()

    target = destination_parent / source.name
    target_file = target / source_file.name

    print("\n===== 診断結果 =====")

    replaced = bool(replacement_done)
    replacement_is_current = (
        source_file.exists()
        and source_file.stat().st_ino == replacement_inode
    )
    copy_completed = len(completed_events) == 1
    copy_failed = len(failed_events) == 1
    target_has_replacement = (
        target_file.exists()
        and target_file.read_bytes() == replacement_content
    )
    target_has_original = (
        target_file.exists()
        and target_file.read_bytes() == original_content
    )

    print(f"[{'PASS' if replaced else 'FAIL'}] 読み取り開始前の置換操作")
    print(f"[{'PASS' if replacement_is_current else 'FAIL'}] コピー元パスが置換後 inode を参照")
    print(f"[{'PASS' if copy_completed else 'INFO'}] コピー成功通知: {len(completed_events)} 回")
    print(f"[{'PASS' if copy_failed else 'INFO'}] コピー失敗通知: {len(failed_events)} 回")
    print(f"[{'INFO' if target_has_replacement else 'INFO'}] コピー先が置換後データと一致")
    print(f"[{'INFO' if target_has_original else 'INFO'}] コピー先が元データと一致")

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )
    print(f"[{'PASS' if not staging_dirs else 'FAIL'}] ステージング領域の清掃")

    print("\n===== 判定 =====")

    if (
        replaced
        and replacement_is_current
        and copy_completed
        and not copy_failed
        and target_has_replacement
    ):
        print("[再現] 置換後のファイルがコピーされ、成功扱いになりました。")
        print("[注意] SHA-256検証が有効でも、置換前の inode との同一性は確認されていません。")
        result = 0
    elif (
        replaced
        and replacement_is_current
        and copy_failed
        and not copy_completed
        and not target.exists()
        and not staging_dirs
    ):
        print("[保護動作] 置換後のファイルを公開せず、安全に失敗しました。")
        result = 0
    else:
        print("[要調査] 想定外の結果です。イベント・コピー先・清掃状態を確認してください。")
        result = 1

    if completed_events:
        print("\n===== 成功通知 =====")
        print(completed_events[0])

    if failed_events:
        print("\n===== 失敗通知 =====")
        print(failed_events[0])

    print("\n===== 関連ログ =====")
    for line in logs:
        print(line)

    print("\n" + "=" * 72)
    print(f"診断試験終了: {'正常終了' if result == 0 else '要調査'}")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return result


if __name__ == "__main__":
    raise SystemExit(main_test())
