from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 ハードリンク経路のコピー元変更試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="hardlink_source_mutation_after_hash_",
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

    # 最初のファイルを先に作り、次に同じinodeへのハードリンクを作る。
    original_file = source / "a_original.bin"
    hardlink_file = source / "b_hardlink.bin"

    original_content = b"HARDLINK-ORIGINAL-CONTENT-" * 40000
    modified_prefix = (
        b"HARDLINK-MODIFIED-AFTER-HASH\n" * 200
    )[:4096]

    original_file.write_bytes(original_content)
    os.link(original_file, hardlink_file)

    original_stat = original_file.stat()
    hardlink_stat = hardlink_file.stat()

    same_inode = (
        original_stat.st_dev == hardlink_stat.st_dev
        and original_stat.st_ino == hardlink_stat.st_ino
    )

    print("\n===== コピー元ハードリンク確認 =====")
    print(f"original inode : {original_stat.st_ino}")
    print(f"hardlink inode : {hardlink_stat.st_ino}")
    print(f"link count     : {original_stat.st_nlink}")

    original_sha256_file = main.sha256_file
    hardlink_hash_completed = []

    def controlled_sha256_file(path, checkpoint):
        digest = original_sha256_file(path, checkpoint)

        if (
            Path(path) == hardlink_file
            and not hardlink_hash_completed
        ):
            print("\n[試験制御] 2つ目のコピー元のSHA-256計算が完了しました。")
            print("[試験操作] コピー先のSHA-256計算前に共有inodeの内容を変更します。")

            with hardlink_file.open("r+b", buffering=0) as stream:
                stream.seek(0)
                written = stream.write(modified_prefix)

                if written != len(modified_prefix):
                    raise RuntimeError("コピー元の変更が不完全です。")

                stream.flush()

            hardlink_hash_completed.append(True)

            if hardlink_file.read_bytes()[:4096] != modified_prefix:
                raise RuntimeError("コピー元の変更を確認できません。")

            print("[確認] ハードリンクされた実体の変更が完了しました。")

        return digest

    main.sha256_file = controlled_sha256_file

    # この試験は a_original.bin → b_hardlink.bin の順に処理されることが前提。
    # os.scandir() の順序はファイルシステム依存（ext4 ではハッシュ順）のため、
    # 試験中は名前順に固定する。os.walk() が next() を使うので、
    # イテレーターとして振る舞う必要がある。
    original_scandir = main.os.scandir

    class SortedScandir:
        def __init__(self, directory):
            with original_scandir(directory) as scan:
                self._entries = iter(sorted(scan, key=lambda entry: entry.name))

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, exc_traceback):
            self.close()
            return False

        def close(self):
            self._entries = iter(())

    main.os.scandir = SortedScandir

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

    try:
        print("\n===== CopyWorker実行 =====")
        worker.run()
    finally:
        main.sha256_file = original_sha256_file
        main.os.scandir = original_scandir

    target_root = destination_parent / source.name

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== ハードリンク経路の検証結果 =====")

    check(
        "コピー元2ファイルが同じinodeを共有している",
        same_inode
    )

    check(
        "2つ目のコピー元のSHA-256計算後に変更操作が実行された",
        bool(hardlink_hash_completed)
    )

    check(
        "共有inodeの内容が変更後の内容になっている",
        original_file.read_bytes()[:4096] == modified_prefix
        and hardlink_file.read_bytes()[:4096] == modified_prefix
    )

    check(
        "ハードリンク経路専用のSHA-256不一致を検出した",
        any(
            "ハードリンク元のSHA-256検証不一致" in str(event)
            for event in failed_events
        )
        or any(
            "ハードリンク元のSHA-256検証不一致" in line
            for line in logs
        )
    )

    check(
        "失敗通知が1回だけ発生した",
        len(failed_events) == 1
    )

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "最終コピー先が公開されていない",
        not target_root.exists()
    )

    staging_dirs = list(
        destination_parent.glob(".ultracopy-staging-*")
    )

    check(
        "一時コピー領域が削除されている",
        len(staging_dirs) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "一時ファイル（.partial）が残っていない",
        len(partial_files) == 0
    )

    if failed_events:
        print("\n===== 失敗通知 =====")
        print(failed_events[0])

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "ハードリンク",
            "SHA-256",
            "エラー",
            "清掃",
        )):
            print(line)

    passed = sum(checks)
    total = len(checks)

    print("\n" + "=" * 72)
    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
