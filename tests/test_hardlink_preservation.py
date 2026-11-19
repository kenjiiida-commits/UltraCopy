from pathlib import Path
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def check(condition, description):
    if condition:
        print(f"[PASS] {description}")
        return True
    print(f"[FAIL] {description}")
    return False


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ハードリンク保持試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="hardlink_preservation_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    original = source / "original.txt"
    hardlink = source / "hardlink.txt"

    original.write_text(
        "UltraCopy hardlink preservation test\n",
        encoding="utf-8"
    )

    # 同じ実体を指すハードリンクを作成
    os.link(original, hardlink)

    source_original_stat = original.stat()
    source_hardlink_stat = hardlink.stat()

    print()
    print("===== コピー元ハードリンク確認 =====")
    print(f"original.txt inode : {source_original_stat.st_ino}")
    print(f"hardlink.txt inode : {source_hardlink_stat.st_ino}")
    print(f"original.txt nlink : {source_original_stat.st_nlink}")
    print(f"hardlink.txt nlink : {source_hardlink_stat.st_nlink}")

    source_is_hardlink = (
        source_original_stat.st_ino == source_hardlink_stat.st_ino
        and source_original_stat.st_dev == source_hardlink_stat.st_dev
    )

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    worker.run()

    copied_root = destination_parent / source.name
    copied_original = copied_root / original.name
    copied_hardlink = copied_root / hardlink.name

    print()
    print("===== コピー先確認 =====")

    if not copied_original.is_file() or not copied_hardlink.is_file():
        print("[FAIL] コピー先のファイルが不足しています")
        for message in failed:
            print(message)
        return 1

    copied_original_stat = copied_original.stat()
    copied_hardlink_stat = copied_hardlink.stat()

    print(f"original.txt inode : {copied_original_stat.st_ino}")
    print(f"hardlink.txt inode : {copied_hardlink_stat.st_ino}")
    print(f"original.txt nlink : {copied_original_stat.st_nlink}")
    print(f"hardlink.txt nlink : {copied_hardlink_stat.st_nlink}")

    destination_is_hardlink = (
        copied_original_stat.st_ino == copied_hardlink_stat.st_ino
        and copied_original_stat.st_dev == copied_hardlink_stat.st_dev
    )

    print()
    print("===== 試験結果 =====")

    results = []

    results.append(check(
        source_is_hardlink,
        "コピー元の2ファイルが同じinodeを共有している"
    ))

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.append(check(
        copied_original.read_bytes() == original.read_bytes()
        and copied_hardlink.read_bytes() == hardlink.read_bytes(),
        "コピー先2ファイルの内容がコピー元と一致した"
    ))

    results.append(check(
        copied_original.read_bytes() == copied_hardlink.read_bytes(),
        "コピー先2ファイルの内容が互いに一致した"
    ))

    results.append(check(
        destination_is_hardlink,
        "コピー先でも2ファイルが同じinodeを共有している"
    ))

    print()
    print("[INFO] コピー先のinodeが異なる場合、")
    print("[INFO] 内容が同じでもハードリンク関係は保持されていません。")

    print()
    print("=" * 70)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 70)

    if failed:
        print("===== エラー =====")
        for message in failed:
            print(message)

    if not all(results):
        print("===== CopyWorkerログ =====")
        for message in logs:
            print(message)
        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
