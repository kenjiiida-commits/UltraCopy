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
    print("UltraCopy Ver.0.7 ハードリンク最終公開直前のコピー元 inode 置換試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="hardlink_source_replacement_before_publish_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"
    source.mkdir()
    destination_parent.mkdir()

    original = source / "a_original.bin"
    hardlink = source / "z_hardlink.bin"
    replacement = run_dir / "replacement.bin"

    original_content = b"ORIGINAL-HARDLINK-SOURCE-" * 100000
    replacement_content = b"REPLACEMENT-HARDLINK-SOURCE-" * 100000

    original.write_bytes(original_content)
    os.link(original, hardlink)
    replacement.write_bytes(replacement_content)

    original_stat = original.stat()
    hardlink_stat = hardlink.stat()
    replacement_stat = replacement.stat()

    print("\n===== コピー元の状態 =====")
    print(f"original inode:   {original_stat.st_ino}")
    print(f"hardlink inode:   {hardlink_stat.st_ino}")
    print(f"replacement inode:{replacement_stat.st_ino}")
    print(f"original nlink:   {original_stat.st_nlink}")

    source_hardlink_valid = (
        original_stat.st_dev == hardlink_stat.st_dev
        and original_stat.st_ino == hardlink_stat.st_ino
    )
    replacement_is_independent = (
        replacement_stat.st_dev != original_stat.st_dev
        or replacement_stat.st_ino != original_stat.st_ino
    )

    if not source_hardlink_valid or not replacement_is_independent:
        print("[FAIL] 試験用 inode 構成が成立していません。")
        return 1

    original_check = main.CopyWorker._check_source_identity
    original_scandir = os.scandir
    check_count = []
    replacement_done = []

    # テスト中だけディレクトリ走査をファイル名順に固定する。
    # os.scandir() 本来のコンテキストマネージャーにも対応させる。
    # os.walk() は next() で1件ずつ取り出すため、本物の os.scandir() と同じく
    # イテレーター（__iter__ と __next__）として振る舞う必要がある。
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

    def sorted_scandir(directory):
        return SortedScandir(directory)

    def controlled_check(worker, path):
        if Path(path) == hardlink:
            check_count.append(True)

        # 先に本来の確認を実行する。
        # ハードリンク経路では6回目が os.link 直前の確認。
        original_check(worker, path)

        if Path(path) == hardlink and len(check_count) == 5:
            if not replacement_done:
                print("\n[試験制御] ハードリンク経路の最終確認直前です。")
                print("[試験操作] コピー元 hardlink.bin を別 inode に置換します。")

                os.replace(replacement, hardlink)
                replacement_done.append(True)

                current_inode = hardlink.stat().st_ino
                print(f"[確認] 置換後 inode: {current_inode}")

                if current_inode != replacement_stat.st_ino:
                    raise RuntimeError("置換後 inode が一致しません。")

    main.CopyWorker._check_source_identity = controlled_check
    main.os.scandir = sorted_scandir

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    try:
        print("\n===== CopyWorker実行 =====")
        worker.run()
    finally:
        main.CopyWorker._check_source_identity = original_check
        main.os.scandir = original_scandir

    copied_root = destination_parent / source.name
    copied_original = copied_root / original.name
    copied_hardlink = copied_root / hardlink.name

    replacement_is_current = (
        hardlink.exists()
        and hardlink.stat().st_ino == replacement_stat.st_ino
    )

    staging_dirs = list(destination_parent.glob(".ultracopy-staging-*"))
    partial_files = list(run_dir.rglob("*.partial"))

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 試験結果 =====")

    check("コピー元2ファイルが当初同じ inode", source_hardlink_valid)
    check("置換用ファイルが別 inode", replacement_is_independent)
    check("ハードリンク経路の最終確認直前まで進んだ",
          len(check_count) == 6)
    check("コピー元の置換操作を実行", bool(replacement_done))
    check("コピー元パスが置換後 inode を参照", replacement_is_current)
    check(
        "ハードリンク作成直前の inode 確認で置換を検出",
        any("置き換えられました" in event for event in failed)
    )
    check("コピー失敗通知が1回", len(failed) == 1)
    check("コピー成功通知なし", len(completed) == 0)
    check("失敗時にコピー先ディレクトリ全体が公開されていない",
          not copied_root.exists())
    check("2つ目のコピー先は公開されていない",
          not copied_hardlink.exists())
    check("ステージング領域の清掃", len(staging_dirs) == 0)
    check("partialファイルが残っていない", len(partial_files) == 0)

    if failed:
        print("\n===== 失敗通知 =====")
        print(failed[0])

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
    try:
        raise SystemExit(main_test())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
