from pathlib import Path
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 FD取得直後・ステージング作成前の競合試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="staging_fd_before_mkdtemp_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    test_data = run_dir / "test_data"
    test_data.mkdir()

    main.DATA_DIR = test_data
    main.DB_PATH = test_data / "history.sqlite3"

    source = run_dir / "source_data"
    destination_parent = run_dir / "destination"
    displaced_parent = run_dir / "destination_displaced"

    source.mkdir()
    destination_parent.mkdir()

    source_file = source / "payload.txt"
    source_content = b"FD BEFORE MKDTEMP RACE TEST\n" * 1024
    source_file.write_bytes(source_content)

    target = destination_parent / source.name

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed_events = []
    failed_events = []
    logs = []

    worker.completed.connect(completed_events.append)
    worker.failed.connect(failed_events.append)
    worker.log.connect(logs.append)

    injection = {
        "executed": False,
        "fd_acquired": False,
        "parent_moved": False,
        "replacement_created": False,
    }

    original_open = main.os.open

    def open_then_replace_parent(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)

        try:
            same_path = Path(path).absolute() == destination_parent.absolute()
        except (TypeError, ValueError):
            same_path = False

        is_directory_open = bool(flags & os.O_DIRECTORY)

        if same_path and is_directory_open and not injection["executed"]:
            injection["fd_acquired"] = True

            print("\n[試験操作] コピー先親ディレクトリのFD取得直後です。")
            print("[試験操作] ステージング領域の作成前に親ディレクトリを移動します。")

            destination_parent.rename(displaced_parent)

            injection["parent_moved"] = (
                displaced_parent.is_dir()
                and not destination_parent.exists()
            )

            print("[試験操作] 元のパスに新しい親ディレクトリを作成します。")
            destination_parent.mkdir()

            injection["replacement_created"] = destination_parent.is_dir()
            injection["executed"] = True

            print("[試験操作] 親ディレクトリの置換が完了しました。")

        return fd

    main.os.open = open_then_replace_parent

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1
    finally:
        main.os.open = original_open

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 競合発生状況 =====")

    check("親ディレクトリのFD取得後に競合注入された",
          injection["fd_acquired"] and injection["executed"])

    check("元の親ディレクトリが別名へ移動された",
          injection["parent_moved"])

    check("元のパスに新しい親ディレクトリが作成された",
          injection["replacement_created"])

    new_staging = list(destination_parent.glob(".ultracopy-staging-*"))
    old_staging = list(displaced_parent.glob(".ultracopy-staging-*"))

    check("置換後の親ディレクトリにステージング領域が作成されていない",
          len(new_staging) == 0)

    print("\n===== 安全性検証 =====")

    check("コピー失敗通知が1回発生した",
          len(failed_events) == 1)

    check("成功通知が発生していない",
          len(completed_events) == 0)

    check("置換後の親ディレクトリに完成品が公開されていない",
          not target.exists())

    check("コピー元ファイルが変更されていない",
          source_file.is_file() and source_file.read_bytes() == source_content)

    print("\n===== 一時領域の残存確認 =====")

    new_staging = list(destination_parent.glob(".ultracopy-staging-*"))
    old_staging = list(displaced_parent.glob(".ultracopy-staging-*"))

    print(f"元の親ディレクトリ: {len(old_staging)} 件")
    for path in old_staging:
        print(f"  {path}")

    print(f"置換後の親ディレクトリ: {len(new_staging)} 件")
    for path in new_staging:
        print(f"  {path}")

    check("元の親ディレクトリにステージング領域が残っていない",
          len(old_staging) == 0)

    check("置換後の親ディレクトリにステージング領域が残っていない",
          len(new_staging) == 0)

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in ("コピー開始", "エラー", "清掃")):
            print(line)

    if failed_events:
        print("\n===== エラー通知内容 =====")
        print(failed_events[0])

    passed = sum(checks)
    total = len(checks)

    print("\n" + "=" * 72)
    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main_test())
