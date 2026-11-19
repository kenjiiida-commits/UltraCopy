from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー先親ディレクトリ・シンボリックリンク試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_parent_symlink_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source_data"
    actual_destination = run_dir / "actual_destination"
    destination_link = run_dir / "destination_link"

    source.mkdir()
    actual_destination.mkdir()

    source_file = source / "test.txt"
    source_content = b"Destination parent symlink test\n" * 100
    source_file.write_bytes(source_content)

    os.symlink(actual_destination, destination_link)

    print("\n===== コピー先の確認 =====")
    print("実体ディレクトリ:", actual_destination)
    print("シンボリックリンク:", destination_link)
    print("リンク先:", os.readlink(destination_link))
    print("is_symlink:", destination_link.is_symlink())
    print("is_dir:", destination_link.is_dir())

    worker = main.CopyWorker(
        source,
        destination_link,
        verify=True
    )

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    print("\n===== CopyWorker実行 =====")
    worker.run()

    target = actual_destination / source.name
    copied_file = target / source_file.name

    print("\n===== 検証結果 =====")

    checks = []

    def check(label, condition):
        condition = bool(condition)
        checks.append(condition)
        print(f"[{'PASS' if condition else 'FAIL'}] {label}")

    check(
        "コピー先親ディレクトリがシンボリックリンクである",
        destination_link.is_symlink()
    )

    check(
        "コピー先親ディレクトリのリンク先が実在する",
        actual_destination.is_dir()
    )

    check(
        "CopyWorkerが正常終了した",
        len(completed) == 1 and len(failed) == 0
    )

    check(
        "コピー先に完成品が作成された",
        target.is_dir()
    )

    check(
        "コピー先ファイルの内容が一致した",
        copied_file.is_file()
        and copied_file.read_bytes() == source_content
    )

    check(
        "コピー元ファイルが変更されていない",
        source_file.read_bytes() == source_content
    )

    check(
        "ステージング領域が残っていない",
        not list(actual_destination.glob(".ultracopy-staging-*"))
    )

    check(
        "コピー失敗通知が発生していない",
        len(failed) == 0
    )

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in ("コピー開始", "エラー", "清掃")):
            print(line)

    if failed:
        print("\n===== エラー通知 =====")
        print(failed[0])

    print("\n" + "=" * 72)
    print(f"試験結果: {sum(checks)} / {len(checks)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
