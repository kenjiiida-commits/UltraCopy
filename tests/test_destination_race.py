from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先競合・上書き防止試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_race_",
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

    source_file = source / "race_test.txt"
    source_content = b"ORIGINAL SOURCE CONTENT\n" * 1024
    source_file.write_bytes(source_content)

    target = destination_parent / source.name
    protected_file = target / "protected.txt"
    protected_content = b"RACE WINNER DATA - DO NOT OVERWRITE\n"

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

    race_injected = []

    original_publish = main.publish_without_overwrite

    def inject_race_then_publish(staged, final_target, *publish_args):
        if not race_injected:
            print(
                "\n[試験操作] 最終公開直前に"
                "競合するコピー先フォルダーを作成します。"
            )

            final_target.mkdir()
            protected_file.write_bytes(protected_content)
            race_injected.append(True)

        # 本物の最終公開処理を呼び出す。
        return original_publish(staged, final_target, *publish_args)

    main.publish_without_overwrite = inject_race_then_publish

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

    print("\n===== 競合状態の検証 =====")

    check(
        "最終公開直前の競合注入が実行された",
        len(race_injected) == 1
    )

    check(
        "競合フォルダーが存在している",
        target.is_dir()
    )

    check(
        "競合フォルダー内の保護ファイルが存在している",
        protected_file.is_file()
    )

    check(
        "保護ファイルの内容が変更されていない",
        protected_file.exists()
        and protected_file.read_bytes() == protected_content
    )

    check(
        "コピー元のファイル内容が競合先に公開されていない",
        not (target / source_file.name).exists()
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
        "ファイル存在競合のエラーが通知された",
        len(failed_events) == 1
        and (
            "FileExistsError" in failed_events[0]
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
