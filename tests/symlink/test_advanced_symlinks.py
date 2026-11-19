from pathlib import Path
import os
import sys
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


def main_test():
    print("=" * 65)
    print("UltraCopy Ver.0.7 高度なシンボリックリンク自動試験")
    print("=" * 65)

    runs_root = PROJECT_ROOT / "tests" / "symlink" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="advanced_symlink_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"
    outside = run_dir / "outside"

    source.mkdir()
    destination_parent.mkdir()
    outside.mkdir()

    # 通常ファイル
    actual = source / "actual"
    actual.mkdir()
    data_file = actual / "data.txt"
    data_file.write_text("CHAIN TARGET DATA\n", encoding="utf-8")

    # 別フォルダーを経由するリンク連鎖
    middle = source / "middle"
    links = source / "links"
    middle.mkdir()
    links.mkdir()

    os.symlink("../actual/data.txt", middle / "link_C")
    os.symlink("../middle/link_C", links / "link_B")
    os.symlink("link_B", links / "link_A")

    # 循環リンク A -> B -> A
    os.symlink("cycle_B", source / "cycle_A")
    os.symlink("cycle_A", source / "cycle_B")

    # コピー元の外部ファイルを指す相対リンク
    outside_file = outside / "external.txt"
    outside_file.write_text("OUTSIDE SOURCE DATA\n", encoding="utf-8")
    os.symlink("../../outside/external.txt", links / "external_relative")

    # 絶対パスで外部ファイルを指すリンク
    os.symlink(str(outside_file), links / "external_absolute")

    # 特殊ファイルへのリンク
    if Path("/dev/null").exists():
        os.symlink("/dev/null", links / "dev_null_link")

    print()
    print("===== コピー元リンク一覧 =====")
    source_links = [
        source / "cycle_A",
        source / "cycle_B",
        links / "link_A",
        links / "link_B",
        middle / "link_C",
        links / "external_relative",
        links / "external_absolute",
    ]

    dev_link = links / "dev_null_link"
    if dev_link.is_symlink():
        source_links.append(dev_link)

    for link in source_links:
        print(f"{link}: {os.readlink(link)}")

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

    target = destination_parent / source.name

    print()
    print("===== コピー結果の検証 =====")

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    # 3段階リンク連鎖
    copied_A = target / "links" / "link_A"
    copied_B = target / "links" / "link_B"
    copied_C = target / "middle" / "link_C"

    results.append(check(
        all(p.is_symlink() for p in (copied_A, copied_B, copied_C)),
        "3段階リンク連鎖がすべてリンクとして複製された"
    ))

    results.append(check(
        os.readlink(copied_A) == "link_B"
        and os.readlink(copied_B) == "../middle/link_C"
        and os.readlink(copied_C) == "../actual/data.txt",
        "3段階リンク連鎖のリンク先文字列がすべて一致した"
    ))

    results.append(check(
        copied_A.resolve() == (target / "actual" / "data.txt").resolve(),
        "コピー先の3段階リンク連鎖がコピー先データに到達する"
    ))

    results.append(check(
        copied_A.read_text(encoding="utf-8") == "CHAIN TARGET DATA\n",
        "コピー先のリンク連鎖を通じてデータを読み取れる"
    ))

    # 循環リンク
    copied_cycle_A = target / "cycle_A"
    copied_cycle_B = target / "cycle_B"

    results.append(check(
        copied_cycle_A.is_symlink() and copied_cycle_B.is_symlink(),
        "循環リンク2個がリンクとして複製された"
    ))

    results.append(check(
        os.readlink(copied_cycle_A) == "cycle_B"
        and os.readlink(copied_cycle_B) == "cycle_A",
        "循環リンクの相互参照文字列が一致した"
    ))

    # 外部相対リンク
    copied_external_relative = target / "links" / "external_relative"

    results.append(check(
        copied_external_relative.is_symlink()
        and os.readlink(copied_external_relative)
        == "../../outside/external.txt",
        "外部相対リンクがリンク文字列を維持して複製された"
    ))

    results.append(check(
        outside_file.exists()
        and not (target / "outside").exists(),
        "外部リンク先の実体をコピー先へ勝手に複製していない"
    ))

    results.append(check(
        not copied_external_relative.exists(),
        "コピー先の外部相対リンクが参照先不在として解決される"
    ))

    # 外部絶対リンク
    copied_external_absolute = target / "links" / "external_absolute"

    results.append(check(
        copied_external_absolute.is_symlink()
        and os.readlink(copied_external_absolute) == str(outside_file),
        "外部絶対リンクのリンク先文字列が一致した"
    ))

    results.append(check(
        copied_external_absolute.exists()
        and copied_external_absolute.read_text(encoding="utf-8")
        == "OUTSIDE SOURCE DATA\n",
        "外部絶対リンクが元の外部ファイルを参照できる"
    ))

    # /dev/nullリンク
    if dev_link.is_symlink():
        copied_dev = target / "links" / "dev_null_link"
        results.append(check(
            copied_dev.is_symlink()
            and os.readlink(copied_dev) == "/dev/null",
            "/dev/nullへのリンクがリンクとして複製された"
        ))

    # 履歴
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
    print("=" * 65)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 65)

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
