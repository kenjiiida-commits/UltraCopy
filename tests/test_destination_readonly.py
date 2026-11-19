from pathlib import Path
import errno
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先読み取り専用エラー試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_readonly_",
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

    source_file = source / "readonly_test.txt"
    source_content = b"ORIGINAL READONLY TEST DATA\n" * 1024
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
        "partial_written": False,
        "ero_fs": False,
    }

    original_copy_stream = worker._copy_stream

    def simulate_readonly_filesystem(src, dst):
        dst = Path(dst)

        print("\n[試験操作] 一時ファイルにテストデータを書き込みます。")

        # 実際の一時ファイルに一部のデータを書き込む。
        with dst.open("wb") as output:
            output.write(b"PARTIAL DATA BEFORE EROFS\n")
            output.flush()

        injection["executed"] = True
        injection["partial_written"] = (
            dst.is_file()
            and dst.stat().st_size > 0
        )

        print("[試験操作] EROFS（読み取り専用）エラーを発生させます。")

        injection["ero_fs"] = True
        raise OSError(
            errno.EROFS,
            "Read-only file system",
            str(dst)
        )

    worker._copy_stream = simulate_readonly_filesystem

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1
    finally:
        worker._copy_stream = original_copy_stream

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 読み取り専用エラーの検証 =====")

    check(
        "エラー注入処理が実行された",
        injection["executed"]
    )

    check(
        "一時ファイルへの部分書き込みが行われた",
        injection["partial_written"]
    )

    check(
        "EROFSエラーが発生した",
        injection["ero_fs"]
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
        "読み取り専用エラーが正しく通知された",
        len(failed_events) == 1
        and "[Errno 30]" in failed_events[0]
        and "Read-only file system" in failed_events[0]
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
