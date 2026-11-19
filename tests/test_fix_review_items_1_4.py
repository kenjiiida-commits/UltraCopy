"""レビュー指摘 1〜4 の修正確認テスト。

1. ハードリンク不可環境でも、検証済みファイルがそのまま公開され、日時・権限が保たれる
2. 読み取り専用フォルダーを含むコピーが失敗・中止しても、一時領域が残らない
   読み取り専用のルートフォルダーもコピーできる
3. 異常終了で残った一時領域を検出・削除できる（使用中のものは対象外）
4. Python 3.11 専用の shutil.rmtree(dir_fd=...) に依存しない

一般ユーザーで実行してください（root では権限の問題が再現しません）。
"""
from pathlib import Path
import errno
import fcntl
import os
import shutil
import stat
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


def run_worker(source, destination_parent, verify=True, before_run=None):
    worker = main.CopyWorker(source, destination_parent, verify)
    completed, failed, logs = [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    if before_run:
        before_run(worker)
    worker.run()
    return worker, completed, failed, logs


def staging_left(destination_parent):
    return [
        p.name for p in destination_parent.iterdir()
        if p.name.startswith(main.STAGING_PREFIX)
    ]


def make_writable(path):
    for root, dirs, _ in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o755)
            except OSError:
                pass


def test_link_unavailable_keeps_metadata(base):
    print("\n--- 1. ハードリンク不可（FAT/exFAT 相当）でも属性が保たれる ---")
    src = base / "t1_src"
    src.mkdir()
    f = src / "data.bin"
    f.write_bytes(os.urandom(300_000))
    os.chmod(f, 0o640)
    os.utime(f, ns=(1_500_000_000_123_456_789, 1_600_000_000_987_654_321))
    hl = src / "data_link.bin"
    os.link(f, hl)
    dst = base / "t1_dst"
    dst.mkdir()

    real_link = os.link

    def no_link(*args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted (simulated FAT)")

    main.os.link = no_link
    try:
        _, completed, failed, _ = run_worker(src, dst)
    finally:
        main.os.link = real_link

    out = dst / "t1_src" / "data.bin"
    check(completed and not failed, "コピーが成功した")
    if out.exists():
        st = out.stat()
        check(stat.S_IMODE(st.st_mode) == 0o640, "権限 0640 が保たれた")
        check(st.st_mtime_ns == 1_600_000_000_987_654_321, "更新日時がナノ秒単位で保たれた")
        check(out.read_bytes() == f.read_bytes(), "内容が一致した")
        check((dst / "t1_src" / "data_link.bin").read_bytes() == f.read_bytes(),
              "ハードリンク側も通常コピーで内容が一致した")
    check(not list(dst.rglob("*.partial")), ".partial ファイルが残っていない")
    check(not staging_left(dst), "一時コピー領域が残っていない")


def test_readonly_dir_failure_cleanup(base):
    print("\n--- 2a. 読み取り専用フォルダーを含むコピーが途中で失敗 ---")
    if os.geteuid() == 0:
        print("[INFO] root は権限 0000 のフォルダーも読めるため、この確認は省略します。")
        return
    src = base / "t2a_src"
    (src / "a_readonly").mkdir(parents=True)
    (src / "a_readonly" / "f.txt").write_text("x")
    (src / "a_readonly" / "inner").mkdir()
    os.chmod(src / "a_readonly" / "inner", 0o000)  # 読み取り不可 → 失敗させる
    os.chmod(src / "a_readonly", 0o555)
    dst = base / "t2a_dst"
    dst.mkdir()
    _, completed, failed, logs = run_worker(src, dst)
    check(failed and not completed, "コピーはエラーで停止した")
    check(not staging_left(dst), "一時コピー領域が残っていない")
    check(not any("清掃に失敗" in m for m in logs), "清掃失敗の警告が出ていない")
    os.chmod(src / "a_readonly", 0o755)
    os.chmod(src / "a_readonly" / "inner", 0o755)


def test_readonly_dir_cancel_cleanup(base):
    print("\n--- 2b. 読み取り専用フォルダーのコピー後に中止 ---")
    src = base / "t2b_src"
    (src / "a_ro").mkdir(parents=True)
    (src / "a_ro" / "f.txt").write_text("x")
    os.chmod(src / "a_ro", 0o555)
    (src / "b_big.bin").write_bytes(os.urandom(1_000_000))
    dst = base / "t2b_dst"
    dst.mkdir()

    def cancel_on_big(worker):
        original = worker._copy_file

        def wrapped(s, d):
            if Path(s).name == "b_big.bin":
                worker.cancel()
            return original(s, d)

        worker._copy_file = wrapped
        original_scandir = main.os.scandir

        def sorted_scandir(p):
            entries = sorted(original_scandir(p), key=lambda e: e.name)

            class _It:
                def __enter__(self):
                    return iter(entries)

                def __exit__(self, *a):
                    return False

            return _It()

        main.os.scandir = sorted_scandir
        worker._restore = lambda: setattr(main.os, "scandir", original_scandir)

    worker, completed, failed, logs = run_worker(src, dst, before_run=cancel_on_big)
    worker._restore()
    check(failed and not completed, "コピーは中止された")
    check(not staging_left(dst), "一時コピー領域が残っていない")
    os.chmod(src / "a_ro", 0o755)


def test_readonly_root_dir(base):
    print("\n--- 2c. 読み取り専用のルートフォルダーをコピー ---")
    src = base / "t2c_src"
    (src / "sub_ro").mkdir(parents=True)
    (src / "sub_ro" / "f.txt").write_text("hello")
    os.chmod(src / "sub_ro", 0o500)
    os.utime(src / "sub_ro", ns=(1_000_000_000_000_000_000, 1_100_000_000_000_000_000))
    os.chmod(src, 0o555)
    os.utime(src, ns=(1_200_000_000_000_000_000, 1_300_000_000_000_000_000))
    dst = base / "t2c_dst"
    dst.mkdir()
    _, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "読み取り専用フォルダーのコピーが成功した")
    out = dst / "t2c_src"
    if out.exists():
        st = out.stat()
        check(stat.S_IMODE(st.st_mode) == 0o555, "ルートフォルダーの権限 0555 が設定された")
        check(st.st_mtime_ns == 1_300_000_000_000_000_000, "ルートフォルダーの更新日時が保たれた")
        sst = (out / "sub_ro").stat()
        check(stat.S_IMODE(sst.st_mode) == 0o500, "サブフォルダーの権限 0500 が設定された")
        check(sst.st_mtime_ns == 1_100_000_000_000_000_000, "サブフォルダーの更新日時が保たれた")
        check((out / "sub_ro" / "f.txt").read_text() == "hello", "中身が一致した")
    check(not staging_left(dst), "一時コピー領域が残っていない")
    os.chmod(src, 0o755)
    os.chmod(src / "sub_ro", 0o755)


def test_readonly_dir_publish_failure_cleanup(base):
    print("\n--- 2d. 属性設定後（公開時）に失敗しても一時領域が残らない ---")
    src = base / "t2d_src"
    (src / "ro" / "deeper").mkdir(parents=True)
    (src / "ro" / "deeper" / "f.txt").write_text("x")
    os.chmod(src / "ro" / "deeper", 0o555)
    os.chmod(src / "ro", 0o555)
    dst = base / "t2d_dst"
    dst.mkdir()

    original_publish = main.publish_without_overwrite

    def collide(staged, target, *args):
        # 公開直前に、別の処理が同名フォルダーを作った状況を再現する。
        target.mkdir()
        return original_publish(staged, target, *args)

    main.publish_without_overwrite = collide
    try:
        _, completed, failed, logs = run_worker(src, dst)
    finally:
        main.publish_without_overwrite = original_publish
    check(failed and not completed, "同名項目があるため公開せずに停止した")
    check(not staging_left(dst), "読み取り専用フォルダーを含む一時領域が残っていない")
    check(not any("清掃に失敗" in m for m in logs), "清掃失敗の警告が出ていない")
    os.chmod(src / "ro", 0o755)
    os.chmod(src / "ro" / "deeper", 0o755)


def test_stale_staging(base):
    print("\n--- 3. 残った一時コピー領域の検出と削除 ---")
    dst = base / "t3_dst"
    dst.mkdir()
    old = time.time() - 3600

    stale = dst / (main.STAGING_PREFIX + "stale1")
    (stale / "x" / "ro").mkdir(parents=True)
    (stale / "x" / "ro" / "data.bin").write_bytes(b"a" * 1000)
    os.chmod(stale / "x" / "ro", 0o000)
    os.utime(stale, (old, old))

    outside = base / "t3_outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    os.symlink(outside, stale / "link_to_outside")
    os.utime(stale, (old, old))

    busy = dst / (main.STAGING_PREFIX + "busy1")
    busy.mkdir()
    os.utime(busy, (old, old))
    busy_fd = os.open(busy, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(busy_fd, fcntl.LOCK_EX)

    fresh = dst / (main.STAGING_PREFIX + "fresh1")
    fresh.mkdir()

    found = main.find_stale_staging(dst)
    names = [n for n, _ in found]
    check(names == [stale.name], f"残骸だけを検出した（使用中・作成直後は除外）: {names}")

    errors = main.remove_stale_staging(dst, names + [busy.name])
    check(not stale.exists(), "残骸（読み取り不可フォルダーを含む）を削除できた")
    check(busy.exists(), "使用中（ロック中）の一時領域は削除されなかった")
    check(any("使用中" in e for e in errors), "使用中のものはエラーとして報告された")
    check((outside / "keep.txt").exists(), "シンボリックリンク先（外部）は削除されなかった")
    check(fresh.exists(), "作成直後の一時領域は残された")
    os.close(busy_fd)


def test_no_py311_rmtree_dependency():
    print("\n--- 4. Python 3.11 専用機能への依存 ---")
    source = (PROJECT_ROOT / "src" / "main.py").read_text(encoding="utf-8")
    check("rmtree(" not in source.replace("shutil.rmtree(dir_fd", ""),
          "shutil.rmtree を使っていない")
    print(f"[INFO] 実行中の Python: {sys.version.split()[0]}")


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 1〜4 修正確認テスト")
    print("=" * 72)
    if os.geteuid() == 0:
        print("[注意] root で実行しています。権限関連の確認は不完全になります。")

    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="fix_review_1_4_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    try:
        test_link_unavailable_keeps_metadata(base)
        test_readonly_dir_failure_cleanup(base)
        test_readonly_dir_cancel_cleanup(base)
        test_readonly_root_dir(base)
        test_readonly_dir_publish_failure_cleanup(base)
        test_stale_staging(base)
        test_no_py311_rmtree_dependency()
    finally:
        make_writable(base)
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
