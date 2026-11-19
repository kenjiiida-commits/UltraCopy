from pathlib import Path
import os
import stat
import sys
import tempfile
import sqlite3
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
    print("UltraCopy Ver.0.7 通常ファイル基本属性自動試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="basic_attributes_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    source_file = source / "attribute_test.txt"
    source_file.write_text(
        "UltraCopy file attribute test\n",
        encoding="utf-8"
    )

    # 通常ファイルの属性を設定
    os.chmod(source_file, 0o640)

    fixed_atime_ns = 1609459200000000000
    fixed_mtime_ns = 1577836800000000000

    os.utime(
        source_file,
        ns=(fixed_atime_ns, fixed_mtime_ns)
    )

    source_stat = source_file.stat()

    source_mode = stat.S_IMODE(source_stat.st_mode)
    source_uid = source_stat.st_uid
    source_gid = source_stat.st_gid
    source_atime_ns = source_stat.st_atime_ns
    source_mtime_ns = source_stat.st_mtime_ns

    print()
    print("===== コピー元属性 =====")
    print(f"UID   : {source_uid}")
    print(f"GID   : {source_gid}")
    print(f"Mode  : {oct(source_mode)}")
    print(f"ATime : {source_atime_ns}")
    print(f"MTime : {source_mtime_ns}")

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

    copied_file = (
        destination_parent / source.name / source_file.name
    )

    if not copied_file.is_file():
        print("[FAIL] コピー先ファイルが存在しません")
        for message in failed:
            print(message)
        return 1

    copied_stat = copied_file.stat()

    copied_mode = stat.S_IMODE(copied_stat.st_mode)
    copied_uid = copied_stat.st_uid
    copied_gid = copied_stat.st_gid
    copied_atime_ns = copied_stat.st_atime_ns
    copied_mtime_ns = copied_stat.st_mtime_ns

    print()
    print("===== コピー先属性 =====")
    print(f"UID   : {copied_uid}")
    print(f"GID   : {copied_gid}")
    print(f"Mode  : {oct(copied_mode)}")
    print(f"ATime : {copied_atime_ns}")
    print(f"MTime : {copied_mtime_ns}")

    print()
    print("===== 属性比較 =====")

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.append(check(
        source_file.read_bytes() == copied_file.read_bytes(),
        "ファイル内容が一致した"
    ))

    results.append(check(
        source_mode == copied_mode,
        "アクセス権限が一致した"
    ))

    results.append(check(
        source_uid == copied_uid,
        "所有者UIDが一致した"
    ))

    results.append(check(
        source_gid == copied_gid,
        "グループGIDが一致した"
    ))

    results.append(check(
        source_mtime_ns == copied_mtime_ns,
        "MTimeがナノ秒単位で一致した"
    ))

    # ATimeは検証中の読み取りでも変化するため参考表示のみ
    print()
    print("===== ATime参考値 =====")
    print(f"コピー元: {source_atime_ns}")
    print(f"コピー先: {copied_atime_ns}")
    print("[INFO] ATimeは読み取り操作などでも変化するため参考値です")

    history_ok = False

    if main.DB_PATH.exists():
        with sqlite3.connect(main.DB_PATH) as db:
            rows = db.execute(
                "SELECT result FROM copy_history"
            ).fetchall()

        history_ok = len(rows) == 1 and rows[0][0] == "成功"

    results.append(check(
        history_ok,
        "試験専用DBに成功履歴が1件記録された"
    ))

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
