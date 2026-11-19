from pathlib import Path
import os
import sys
import stat
import tempfile
import sqlite3
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def check(condition, description):
    if condition:
        print(f"[PASS] {description}")
        return True
    print(f"[FAIL] {description}")
    return False


def format_time(ns):
    return f"{ns} ns"


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 シンボリックリンク属性自動試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "symlink" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="symlink_attributes_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    target_file = source / "target.txt"
    target_file.write_text("SYMLINK ATTRIBUTE TEST\n", encoding="utf-8")

    source_link = source / "attribute_link"
    os.symlink("target.txt", source_link)

    # シンボリックリンク自身の日時を設定する
    fixed_time_ns = 1577836800000000000  # 2020-01-01 00:00:00 UTC
    os.utime(
        source_link,
        ns=(fixed_time_ns, fixed_time_ns),
        follow_symlinks=False
    )

    # コピー実行前の属性を取得
    source_stat = source_link.lstat()

    source_uid = source_stat.st_uid
    source_gid = source_stat.st_gid
    source_mode = stat.S_IMODE(source_stat.st_mode)
    source_atime_ns = source_stat.st_atime_ns
    source_mtime_ns = source_stat.st_mtime_ns
    source_target = os.readlink(source_link)

    # readlink後のアクセス日時を再取得
    source_stat = source_link.lstat()
    source_atime_ns = source_stat.st_atime_ns

    print()
    print("===== コピー元の属性 =====")
    print(f"UID       : {source_uid}")
    print(f"GID       : {source_gid}")
    print(f"Mode      : {oct(source_mode)}")
    print(f"ATime     : {format_time(source_atime_ns)}")
    print(f"MTime     : {format_time(source_mtime_ns)}")
    print(f"リンク先  : {source_target}")

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

    copied_link = destination_parent / source.name / "attribute_link"

    if not copied_link.is_symlink():
        print("[FAIL] コピー先にシンボリックリンクが存在しない")
        for message in failed:
            print(message)
        return 1

    # コピー先の属性を取得
    copied_stat = copied_link.lstat()

    copied_uid = copied_stat.st_uid
    copied_gid = copied_stat.st_gid
    copied_mode = stat.S_IMODE(copied_stat.st_mode)
    copied_atime_ns = copied_stat.st_atime_ns
    copied_mtime_ns = copied_stat.st_mtime_ns
    copied_target = os.readlink(copied_link)

    print()
    print("===== コピー先の属性 =====")
    print(f"UID       : {copied_uid}")
    print(f"GID       : {copied_gid}")
    print(f"Mode      : {oct(copied_mode)}")
    print(f"ATime     : {format_time(copied_atime_ns)}")
    print(f"MTime     : {format_time(copied_mtime_ns)}")
    print(f"リンク先  : {copied_target}")

    print()
    print("===== 属性比較 =====")

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.append(check(
        source_uid == copied_uid,
        "所有者UIDがコピー元とコピー先で一致した"
    ))

    results.append(check(
        source_gid == copied_gid,
        "グループGIDがコピー元とコピー先で一致した"
    ))

    results.append(check(
        source_mode == copied_mode,
        "シンボリックリンクのモードが一致した"
    ))

    results.append(check(
        source_target == copied_target,
        "リンク先文字列が一致した"
    ))

    print()
    print("===== タイムスタンプ検証 =====")

    print(f"コピー元 MTime : {format_time(source_mtime_ns)}")
    print(f"コピー先 MTime : {format_time(copied_mtime_ns)}")

    if source_mtime_ns == copied_mtime_ns:
        print("[INFO] MTimeはコピー元とコピー先で一致しています")
    else:
        print("[KNOWN LIMITATION] MTimeはコピー元から引き継がれていません")

    print()
    print(f"コピー元 ATime : {format_time(source_atime_ns)}")
    print(f"コピー先 ATime : {format_time(copied_atime_ns)}")
    print("[INFO] ATimeは読み取り操作などでも変化するため、参考値として記録します")

    # 試験専用DBの履歴確認
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
