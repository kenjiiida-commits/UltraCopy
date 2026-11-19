from pathlib import Path
import os
import stat
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先アクセス権限変更試験")
    print("=" * 72)

    if os.geteuid() == 0:
        print("試験中止：root権限ではアクセス権限の検証が適切に行えません。")
        return 1

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_permission_",
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

    source_file = source / "permission_test.txt"
    source_content = b"ORIGINAL PERMISSION TEST DATA\n" * 1024
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
        "permission_denied": False,
        "restored": False,
    }

    original_publish = main.publish_without_overwrite

    def change_permission_then_publish(staged, final_target, *publish_args):
        if not injection["executed"]:
            parent = Path(final_target).parent

            print("\n[試験操作] コピー先親ディレクトリを0555に変更します。")

            parent.chmod(0o555)
            injection["executed"] = True

            try:
                return original_publish(staged, final_target, *publish_args)
            except PermissionError:
                injection["permission_denied"] = True
                raise
            finally:
                # 試験専用ディレクトリの権限を必ず復旧する。
                parent.chmod(0o755)
                injection["restored"] = (
                    stat.S_IMODE(parent.stat().st_mode) == 0o755
                )
                print("[試験操作] コピー先親ディレクトリを0755に復旧しました。")

        return original_publish(staged, final_target, *publish_args)

    main.publish_without_overwrite = change_permission_then_publish

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1
    finally:
        main.publish_without_overwrite = original_publish
        # 万一、権限復旧に失敗していた場合にも再度復旧を試みる。
        if destination_parent.exists():
            destination_parent.chmod(0o755)

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== アクセス権限変更の検証 =====")

    check(
        "権限変更処理が実行された",
        injection["executed"]
    )

    check(
        "公開処理でPermissionErrorが発生した",
        injection["permission_denied"]
    )

    check(
        "コピー先親ディレクトリの権限が0755に復旧した",
        injection["restored"]
        and stat.S_IMODE(destination_parent.stat().st_mode) == 0o755
    )

    check(
        "コピー先に完成品が公開されていない",
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
        "アクセス権限エラーが通知された",
        len(failed_events) == 1
        and "PermissionError" in failed_events[0]
        and (
            "Permission denied" in failed_events[0]
            or "[Errno 13]" in failed_events[0]
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
