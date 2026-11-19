from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先ファイル名競合試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_file_collision_",
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

    source_file = source / "collision_test.txt"
    source_content = b"ORIGINAL SOURCE CONTENT\n" * 1024
    source_file.write_bytes(source_content)

    target = destination_parent / source.name

    competing_content = b"COMPETING PROCESS CONTENT\n" * 512

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
        "target_created": False,
    }

    original_publish = main.publish_without_overwrite

    def create_competing_file_then_publish(staged, final_target, *publish_args):
        if not injection["executed"]:
            final_target = Path(final_target)

            print("\n[試験操作] 最終公開直前に競合ファイルを作成します。")

            # 排他的作成モードで競合ファイルを作る。
            with final_target.open("xb") as output:
                output.write(competing_content)

            injection["executed"] = True
            injection["target_created"] = (
                final_target.is_file()
                and final_target.read_bytes() == competing_content
            )

            print("[試験操作] 競合ファイルを作成しました。")

        return original_publish(staged, final_target, *publish_args)

    main.publish_without_overwrite = create_competing_file_then_publish

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

    print("\n===== ファイル名競合の検証 =====")

    check(
        "競合ファイル作成処理が実行された",
        injection["executed"]
    )

    check(
        "競合ファイルが正しい内容で作成された",
        injection["target_created"]
    )

    check(
        "コピー先の競合ファイルが存在している",
        target.is_file()
    )

    check(
        "競合ファイルの内容が上書きされていない",
        target.is_file()
        and target.read_bytes() == competing_content
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
        "ファイル競合エラーが通知された",
        len(failed_events) == 1
        and (
            "[Errno 17]" in failed_events[0]
            or "File exists" in failed_events[0]
        )
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "ステージング領域が清掃されている",
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
