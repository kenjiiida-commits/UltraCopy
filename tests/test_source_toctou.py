"""レビュー指摘 5（コピー元側の TOCTOU 競合）の確認テスト。

A. コピー中に途中のフォルダーがシンボリックリンクに差し替えられても、
   コピー元ツリーの外のファイルを読まない
B. フォルダーを確認した直後・開く直前にシンボリックリンクへ差し替えられたら停止する
C. フォルダーを確認した直後・開く直前に別のフォルダーへ差し替えられたら停止する
D. 通常ファイルが FIFO に差し替えられても、待ち続けずにエラーになる
E. 利用者が指定したコピー元のパスの途中にシンボリックリンクがあっても、正常にコピーできる
"""
from pathlib import Path
import os
import shutil
import signal
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []
SECRET = b"SECRET-OUTSIDE-DATA"


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


class Timeout(BaseException):
    pass


def run_worker(source, destination_parent, before_run=None, timeout=20):
    worker = main.CopyWorker(source, destination_parent, True)
    completed, failed, logs = [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    if before_run:
        before_run(worker)

    def on_alarm(signum, frame):
        raise Timeout()

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(timeout)
    hung = False
    try:
        worker.run()
    except Timeout:
        hung = True
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return completed, failed, logs, hung


def tree_contains(root, data):
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink():
            if data in path.read_bytes():
                return True
    return False


def make_outside(base, name):
    outside = base / name
    outside.mkdir()
    (outside / "file.txt").write_bytes(SECRET)
    (outside / "other.txt").write_bytes(SECRET)
    return outside


def test_parent_swapped_during_copy(base):
    print("\n--- A. コピー中に途中のフォルダーをシンボリックリンクへ差し替え ---")
    src = base / "a_src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_bytes(b"ORIGINAL")
    outside = make_outside(base, "a_outside")
    dst = base / "a_dst"
    dst.mkdir()
    swapped = []

    def inject(worker):
        original = worker._copy_file

        def wrapped(s, d):
            if Path(str(s)) == src / "sub" / "file.txt" and not swapped:
                # 識別情報の取得より前に、親フォルダーを外部へのリンクに差し替える。
                os.rename(src / "sub", src / "sub_moved")
                os.symlink(outside, src / "sub")
                swapped.append(True)
            return original(s, d)

        worker._copy_file = wrapped

    completed, failed, _, hung = run_worker(src, dst, inject)
    check(swapped, "差し替え操作が実行された")
    check(not hung, "処理が止まらずに終了した")
    check(not tree_contains(dst, SECRET), "コピー先にツリー外のデータが含まれていない")
    if completed:
        out = dst / "a_src" / "sub" / "file.txt"
        check(out.read_bytes() == b"ORIGINAL",
              "最初に開いたフォルダーの元データがコピーされた")


def _hook_dir_open(target_name, action):
    """コピー元フォルダー target_name を開く直前に action を実行する。"""
    original_open = main.os.open
    state = {"done": False}

    def patched(path, flags, *args, **kwargs):
        if (
            not state["done"]
            and flags & os.O_DIRECTORY
            and isinstance(path, main.AnchoredPath)
            and path.name == target_name
        ):
            state["done"] = True
            action()
        return original_open(path, flags, *args, **kwargs)

    main.os.open = patched
    return state, lambda: setattr(main.os, "open", original_open)


def test_dir_swapped_to_symlink_before_open(base):
    print("\n--- B. フォルダーを開く直前にシンボリックリンクへ差し替え ---")
    src = base / "b_src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_bytes(b"ORIGINAL")
    outside = make_outside(base, "b_outside")
    dst = base / "b_dst"
    dst.mkdir()

    def swap():
        os.rename(src / "sub", src / "sub_moved")
        os.symlink(outside, src / "sub")

    state, restore = _hook_dir_open("sub", swap)
    try:
        completed, failed, _, hung = run_worker(src, dst)
    finally:
        restore()
    check(state["done"], "差し替え操作が実行された")
    check(failed and not completed, "コピーはエラーで停止した")
    check(not tree_contains(dst, SECRET), "ツリー外のデータを読んでいない")
    check(not [p for p in dst.iterdir()], "コピー先に何も公開されていない")


def test_dir_swapped_to_other_dir_before_open(base):
    print("\n--- C. フォルダーを開く直前に別のフォルダーへ差し替え ---")
    src = base / "c_src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_bytes(b"ORIGINAL")
    other = make_outside(base, "c_other")
    dst = base / "c_dst"
    dst.mkdir()

    def swap():
        os.rename(src / "sub", src / "sub_moved")
        os.rename(other, src / "sub")

    state, restore = _hook_dir_open("sub", swap)
    try:
        completed, failed, _, hung = run_worker(src, dst)
    finally:
        restore()
    check(state["done"], "差し替え操作が実行された")
    check(failed and not completed, "コピーはエラーで停止した")
    check(any("置き換えられました" in m for m in failed), "差し替えとして報告された")
    check(not tree_contains(dst, SECRET), "差し替え後のフォルダーの中身を読んでいない")


def test_file_swapped_to_fifo(base):
    print("\n--- D. 通常ファイルを開く直前に FIFO へ差し替え ---")
    src = base / "d_src"
    src.mkdir()
    (src / "file.txt").write_bytes(b"ORIGINAL")
    dst = base / "d_dst"
    dst.mkdir()
    swapped = []

    def inject(worker):
        original = worker._copy_stream

        def wrapped(s, d):
            if Path(str(s)) == src / "file.txt" and not swapped:
                os.unlink(src / "file.txt")
                os.mkfifo(src / "file.txt")
                swapped.append(True)
            return original(s, d)

        worker._copy_stream = wrapped

    completed, failed, _, hung = run_worker(src, dst, inject, timeout=10)
    check(swapped, "差し替え操作が実行された")
    check(not hung, "FIFO を開いても待ち続けなかった")
    check(failed and not completed, "コピーはエラーで停止した")


def test_symlink_in_user_path(base):
    print("\n--- E. 指定したパスの途中にシンボリックリンクがある場合 ---")
    real = base / "e_real"
    (real / "data" / "sub").mkdir(parents=True)
    (real / "data" / "sub" / "f.txt").write_bytes(b"OK")
    link = base / "e_link"
    os.symlink(real, link)
    dst = base / "e_dst"
    dst.mkdir()
    completed, failed, _, hung = run_worker(link / "data", dst)
    check(completed and not failed, "コピーが成功した")
    check((dst / "data" / "sub" / "f.txt").read_bytes() == b"OK", "内容が一致した")


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 5（コピー元 TOCTOU）確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="source_toctou_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    try:
        test_parent_swapped_during_copy(base)
        if hasattr(main, "AnchoredPath"):
            test_dir_swapped_to_symlink_before_open(base)
            test_dir_swapped_to_other_dir_before_open(base)
        else:
            print("\n[INFO] B・C は FD 基準の実装でのみ実行します。")
        test_file_swapped_to_fifo(base)
        test_symlink_in_user_path(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
