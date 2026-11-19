from pathlib import Path
import os
import sys
import sqlite3
import tempfile
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
    print("=" * 60)
    print("UltraCopy Ver.0.7 シンボリックリンク自動試験")
    print("=" * 60)

    runs_root = PROJECT_ROOT / "tests" / "symlink" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="basic_symlink_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    # 試験専用の履歴保存先に切り替える
    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    # 実体となるファイルとフォルダー
    actual_dir = source / "actual_dir"
    actual_dir.mkdir()

    actual_file = source / "actual.txt"
    actual_file.write_text(
        "UltraCopy symbolic link test\n",
        encoding="utf-8"
    )

    nested_file = actual_dir / "inside.txt"
    nested_file.write_text(
        "Directory symlink target\n",
        encoding="utf-8"
    )

    # 4種類のシンボリックリンクを作成
    relative_file_link = source / "relative_file_link"
    os.symlink("actual.txt", relative_file_link)

    absolute_file_link = source / "absolute_file_link"
    os.symlink(str(actual_file), absolute_file_link)

    broken_link = source / "broken_link"
    os.symlink("missing_target.txt", broken_link)

    directory_link = source / "directory_link"
    os.symlink("actual_dir", directory_link)

    print()
    print("===== コピー元リンクの確認 =====")

    for link in (
        relative_file_link,
        absolute_file_link,
        broken_link,
        directory_link,
    ):
        print(
            f"{link.name}: "
            f"is_symlink={link.is_symlink()}, "
            f"target={os.readlink(link)}"
        )

    # CopyWorkerをGUI画面なしで直接実行
    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed_messages = []
    failed_messages = []
    log_messages = []

    worker.completed.connect(completed_messages.append)
    worker.failed.connect(failed_messages.append)
    worker.log.connect(log_messages.append)

    print()
    print("===== CopyWorker実行 =====")

    worker.run()

    target = destination_parent / source.name

    print()
    print("===== コピー結果の検証 =====")

    results = []

    results.append(check(
        not failed_messages and bool(completed_messages),
        "CopyWorkerが正常終了した"
    ))

    copied_relative = target / "relative_file_link"
    copied_absolute = target / "absolute_file_link"
    copied_broken = target / "broken_link"
    copied_directory = target / "directory_link"

    results.append(check(
        copied_relative.is_symlink(),
        "相対パスのリンクがシンボリックリンクとして複製された"
    ))

    results.append(check(
        copied_absolute.is_symlink(),
        "絶対パスのリンクがシンボリックリンクとして複製された"
    ))

    results.append(check(
        copied_broken.is_symlink(),
        "壊れたリンクがシンボリックリンクとして複製された"
    ))

    results.append(check(
        copied_directory.is_symlink(),
        "ディレクトリリンクがシンボリックリンクとして複製された"
    ))

    results.append(check(
        os.readlink(copied_relative) == "actual.txt",
        "相対リンクのリンク先文字列が一致した"
    ))

    results.append(check(
        os.readlink(copied_absolute) == str(actual_file),
        "絶対リンクのリンク先文字列が一致した"
    ))

    results.append(check(
        os.readlink(copied_broken) == "missing_target.txt",
        "壊れたリンクのリンク先文字列が一致した"
    ))

    results.append(check(
        os.readlink(copied_directory) == "actual_dir",
        "ディレクトリリンクのリンク先文字列が一致した"
    ))

    results.append(check(
        (target / "actual.txt").read_text(encoding="utf-8")
        == actual_file.read_text(encoding="utf-8"),
        "通常ファイルの内容が一致した"
    ))

    results.append(check(
        (target / "actual_dir" / "inside.txt").read_text(
            encoding="utf-8"
        ) == nested_file.read_text(encoding="utf-8"),
        "通常フォルダー内のファイルがコピーされた"
    ))

    # 履歴が試験専用DBに記録されたか確認
    history_ok = False

    if main.DB_PATH.exists():
        with sqlite3.connect(main.DB_PATH) as db:
            rows = db.execute(
                "SELECT result FROM copy_history"
            ).fetchall()

        history_ok = len(rows) == 1 and rows[0][0] == "成功"

    results.append(check(
        history_ok,
        "試験専用データベースに成功履歴が1件記録された"
    ))

    print()
    print("=" * 60)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 60)

    if failed_messages:
        print()
        print("===== エラー内容 =====")
        for message in failed_messages:
            print(message)

    if not all(results):
        print()
        print("===== CopyWorkerログ =====")
        for message in log_messages:
            print(message)

        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
