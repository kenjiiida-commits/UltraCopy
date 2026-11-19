from pathlib import Path
import shutil
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先親ディレクトリ削除・再作成試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_parent_removed_",
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

    source_file = source / "parent_test.txt"
    source_content = b"ORIGINAL SOURCE DATA\n" * 1024
    source_file.write_bytes(source_content)

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
        "staged_missing": False,
        "parent_recreated": False,
    }

    original_publish = main.publish_without_overwrite

    def remove_parent_then_publish(staged, final_target, *publish_args):
        if not injection["executed"]:
            staged = Path(staged)
            parent = Path(final_target).parent

            print("\n[試験操作] 最終公開直前に親ディレクトリを削除します。")

            # 試験専用のコピー先親ディレクトリを削除する。
            shutil.rmtree(parent)

            # 同じパスに新しい親ディレクトリを作成する。
            parent.mkdir(parents=True)

            injection["executed"] = True
            injection["staged_missing"] = not staged.exists()
            injection["parent_recreated"] = parent.is_dir()

            print("[試験操作] 親ディレクトリを同じパスに再作成しました。")
            print(
                "[試験操作] ステージング領域の存在:",
                staged.exists()
            )

        # 本物の最終公開処理を呼び出す。
        return original_publish(staged, final_target, *publish_args)

    main.publish_without_overwrite = remove_parent_then_publish

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

    print("\n===== 親ディレクトリ削除・再作成の検証 =====")

    check(
        "親ディレクトリの削除・再作成処理が実行された",
        injection["executed"]
    )

    check(
        "親ディレクトリが同じパスに再作成された",
        injection["parent_recreated"]
        and destination_parent.is_dir()
    )

    check(
        "削除によりステージング領域が失われた",
        injection["staged_missing"]
    )

    check(
        "コピー先に完成品が公開されていない",
        not target.exists()
    )

    check(
        "コピー先親ディレクトリに不要な項目が残っていない",
        destination_parent.is_dir()
        and not any(destination_parent.iterdir())
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
        "ステージング領域消失によるエラーが通知された",
        len(failed_events) == 1
        and "FileNotFoundError" in failed_events[0]
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "ステージングディレクトリが残っていない",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "不完全な一時ファイルが残っていない",
        len(partial_files) == 0
    )

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "エラー",
            "清掃",
            "コピー開始",
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
