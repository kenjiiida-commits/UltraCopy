from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先親ディレクトリ置換試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_parent_replaced_",
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

    source_file = source / "replacement_test.txt"
    source_content = b"ORIGINAL SOURCE CONTENT\n" * 1024
    source_file.write_bytes(source_content)

    competing_content = b"REPLACEMENT REGULAR FILE\n" * 512

    target = destination_parent / source.name

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

    injection = {
        "executed": False,
        "directory_moved": False,
        "regular_file_created": False,
    }

    original_publish = main.publish_without_overwrite

    def replace_parent_then_publish(staged, final_target, *publish_args):
        if not injection["executed"]:
            final_target = Path(final_target)
            parent = final_target.parent

            print("\n[試験操作] コピー先ディレクトリを別名に移動します。")

            parent.rename(displaced_parent)

            injection["directory_moved"] = (
                displaced_parent.is_dir()
                and not parent.exists()
            )

            print("[試験操作] 元のパスに通常ファイルを作成します。")

            with parent.open("xb") as output:
                output.write(competing_content)

            injection["executed"] = True
            injection["regular_file_created"] = (
                parent.is_file()
                and parent.read_bytes() == competing_content
            )

            print("[試験操作] ディレクトリ置換が完了しました。")

        return original_publish(staged, final_target, *publish_args)

    main.publish_without_overwrite = replace_parent_then_publish

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1
    finally:
        main.publish_without_overwrite = original_publish

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 親ディレクトリ置換の検証 =====")

    check(
        "置換処理が実行された",
        injection["executed"]
    )

    check(
        "元のディレクトリが別の名前に移動された",
        injection["directory_moved"]
    )

    check(
        "元のパスに通常ファイルが作成された",
        injection["regular_file_created"]
    )

    check(
        "置換された通常ファイルが存在している",
        destination_parent.is_file()
    )

    check(
        "置換された通常ファイルの内容が保持されている",
        destination_parent.is_file()
        and destination_parent.read_bytes() == competing_content
    )

    check(
        "コピー先の完成品が誤って公開されていない",
        not target.exists()
    )

    check(
        "コピー元ファイルが存在している",
        source_file.is_file()
    )

    check(
        "コピー元ファイルの内容が変更されていない",
        source_file.exists()
        and source_file.read_bytes() == source_content
    )

    check(
        "コピー失敗通知が1回発生した",
        len(failed_events) == 1
    )

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "ディレクトリ置換によるエラーが通知された",
        len(failed_events) == 1
        and (
            "[Errno 20]" in failed_events[0]
            or "Not a directory" in failed_events[0]
            or "NotADirectoryError" in failed_events[0]
        )
    )

    # 移動したディレクトリ内のステージング領域を確認する。
    remaining_staging = list(
        displaced_parent.glob(".ultracopy-staging-*")
    )

    check(
        "移動先にステージング領域が残っていない",
        len(remaining_staging) == 0
    )

    remaining_partials = list(
        displaced_parent.rglob("*.partial")
    )

    check(
        "移動先に不完全な一時ファイルが残っていない",
        len(remaining_partials) == 0
    )

    print("\n===== 清掃状況 =====")

    if remaining_staging:
        print("[確認] 移動先にステージング領域が残っています。")
        for path in remaining_staging:
            print(f"  {path}")
    else:
        print("[確認] 移動先にステージング領域はありません。")

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "コピー開始",
            "エラー",
            "清掃",
        )):
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
