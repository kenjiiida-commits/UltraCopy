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
    print("UltraCopy Ver.0.7 コピー先容量不足（ENOSPC）耐性試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_enospc_",
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

    source_file = source / "capacity_test.bin"
    original_content = b"ENOSPC-TEST-DATA-" * 65536
    source_file.write_bytes(original_content)

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

    simulated_write_bytes = []

    def fail_after_partial_write(src_path, dst_path):
        """
        実ディスクを満杯にせず、
        一時ファイルへの部分書き込み後にENOSPCを発生させる。
        """
        with Path(src_path).open("rb") as rf:
            partial_data = rf.read(4096)

        with Path(dst_path).open("wb") as wf:
            written = wf.write(partial_data)
            wf.flush()

        simulated_write_bytes.append(written)

        print(
            "\n[試験操作] 一時ファイルへ"
            f" {written} bytesを書き込んだ後、"
            "ENOSPCを発生させます。"
        )

        raise OSError(
            errno.ENOSPC,
            "No space left on device"
        )

    worker._copy_stream = fail_after_partial_write

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1

    target = destination_parent / source.name

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 容量不足時の整合性検証 =====")

    check(
        "容量不足を発生させる試験処理が実行された",
        len(simulated_write_bytes) == 1
    )

    check(
        "一時ファイルへの部分書き込みが実行された",
        bool(simulated_write_bytes)
        and simulated_write_bytes[0] > 0
    )

    check(
        "ENOSPC（空き容量不足）がエラー通知に記録された",
        len(failed_events) == 1
        and "No space left on device" in failed_events[0]
    )

    check(
        "失敗通知が発生した",
        len(failed_events) == 1
    )

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "最終コピー先が公開されていない",
        not target.exists()
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "ステージング領域が削除されている",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "不完全な一時ファイルが残っていない",
        len(partial_files) == 0
    )

    check(
        "コピー元ファイルが変更されていない",
        source_file.exists()
        and source_file.read_bytes() == original_content
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
