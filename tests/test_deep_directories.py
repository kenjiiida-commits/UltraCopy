"""非常に深いフォルダー階層への対応の確認テスト。

A. 3000 階層をコピーでき、最下層のファイルとフォルダーの属性も正しい
   （従来は約 1000 階層で RecursionError）
B. 長い日本語の名前が続き、パス全体が 4096 バイトを超えてもコピーできる
   （従来はコピー先のパスが長すぎて File name too long）
C. 開けるファイル数の上限を 64 に下げても、500 階層をコピーできる
   （開いているフォルダーの数が階層の深さに比例しない）
D. 深さの離れたハードリンク（1 階層目と 2000 階層目）も再現される
E. 深い階層の読み取り専用フォルダーの権限が設定される。途中で失敗しても
   一時コピー領域が残らない
F. 深い階層で中止しても、一時コピー領域が残らない
G. 異常終了で残った深い一時コピー領域を削除できる
H. コピー中に、途中のフォルダーが別の場所へ移動されたら停止する
"""
from pathlib import Path
import os
import resource
import shutil
import stat
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


def run_worker(source, destination_parent, before_run=None):
    worker = main.CopyWorker(source, destination_parent, True)
    completed, failed, logs = [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    if before_run:
        before_run(worker)
    worker.run()
    return worker, completed, failed, logs


def make_chain(root, names, files=None, modes=None, mtimes=None):
    """root の下に names の順でフォルダーを作る（パスではなく FD で作る）。

    files: {階層: {名前: 中身}}、modes/mtimes: {階層: 値}（階層は 1 から）
    同時に開くフォルダーは2個まで（一般ユーザーの上限 1024 を超えないように）。
    """
    files, modes, mtimes = files or {}, modes or {}, mtimes or {}
    fd = os.open(root, DIR_FLAGS)
    for level, name in enumerate(names, start=1):
        os.mkdir(name, dir_fd=fd)
        child = os.open(name, DIR_FLAGS, dir_fd=fd)
        os.close(fd)
        fd = child
        for fname, data in files.get(level, {}).items():
            f = os.open(fname, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
            os.write(f, data)
            os.close(f)
    os.close(fd)
    if not modes and not mtimes:
        return
    # 中身を作り終えてから、上の階層から順に権限と日時を設定する。
    # 次の階層を開いてから設定するので、読み取り専用にしても先へ進める。
    fd = os.open(root, DIR_FLAGS)
    for level, name in enumerate(names, start=1):
        child = os.open(name, DIR_FLAGS, dir_fd=fd)
        os.close(fd)
        fd = child
        if level < len(names):
            nxt = os.open(names[level], DIR_FLAGS, dir_fd=fd)
        if level in mtimes:
            os.utime(fd, ns=(mtimes[level], mtimes[level]))
        if level in modes:
            os.chmod(fd, modes[level])
        if level < len(names):
            os.close(fd)
            fd = nxt
            # 次のループで開き直さないよう、1つ戻った状態に合わせる
            parent = os.open("..", DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = parent
    os.close(fd)


def read_chain(root, names):
    """コピー結果を FD でたどり、各階層の (権限, 更新日時, {ファイル名: 中身}) を返す。"""
    result = []
    fd = os.open(root, DIR_FLAGS)
    try:
        for name in names:
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            contents = {}
            for entry in os.scandir(fd):
                if entry.is_file(follow_symlinks=False):
                    f = os.open(entry.name, os.O_RDONLY, dir_fd=fd)
                    contents[entry.name] = (os.read(f, 1 << 20), os.fstat(f).st_ino)
                    os.close(f)
            result.append((stat.S_IMODE(st.st_mode), st.st_mtime_ns, contents))
    finally:
        os.close(fd)
    return result


def make_tree_removable(root):
    """後片付け用：深いツリーを FD でたどって削除する。"""
    try:
        parent = os.open(Path(root).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            main.remove_tree_at(parent, Path(root).name)
        finally:
            os.close(parent)
    except OSError:
        pass


def staging_left(destination):
    return [p.name for p in destination.iterdir() if p.name.startswith(main.STAGING_PREFIX)]


def test_very_deep(base):
    print("\n--- A. 3000 階層 ---")
    src = base / "a_deep"
    src.mkdir()
    names = ["d"] * 3000
    make_chain(src, names,
               files={1: {"top.txt": b"top"}, 1500: {"mid.txt": b"mid"},
                      3000: {"bottom.txt": b"bottom"}},
               modes={3000: 0o750, 2999: 0o710},
               mtimes={3000: 1_500_000_000_000_000_000})
    dst = base / "a_dst"
    dst.mkdir()
    _, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "3000 階層のコピーが完了した")
    if completed:
        levels = read_chain(dst / "a_deep", names)
        check(len(levels) == 3000, "3000 階層すべてが作られた")
        check(levels[0][2].get("top.txt", (None,))[0] == b"top"
              and levels[1499][2].get("mid.txt", (None,))[0] == b"mid"
              and levels[2999][2].get("bottom.txt", (None,))[0] == b"bottom",
              "1・1500・3000 階層目のファイルの中身が正しい")
        check(levels[2999][0] == 0o750 and levels[2998][0] == 0o710,
              "深い階層のフォルダーの権限が設定された")
        check(levels[2999][1] == 1_500_000_000_000_000_000,
              "最下層のフォルダーの更新日時が設定された")
    check(not staging_left(dst), "一時コピー領域が残っていない")


def test_long_names(base):
    print("\n--- B. 長い日本語の名前（パスが 4096 バイトを超える） ---")
    src = base / "b_long"
    src.mkdir()
    name = "とても長い日本語のフォルダー名" * 5          # 75 文字・225 バイト
    names = [f"{i:02d}{name}" for i in range(40)]      # 合計 約 9000 バイト
    total = sum(len(n.encode()) + 1 for n in names)
    make_chain(src, names, files={40: {"最下層のファイル.txt": "中身".encode()}})
    dst = base / "b_dst"
    dst.mkdir()
    _, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, f"パスの長さ 約 {total:,} バイトのコピーが完了した")
    if completed:
        levels = read_chain(dst / "b_long", names)
        check(levels[-1][2].get("最下層のファイル.txt", (None,))[0] == "中身".encode(),
              "最下層のファイルの中身が正しい")


def test_fd_limit(base):
    print("\n--- C. 開けるファイル数の上限を 64 にして 500 階層 ---")
    src = base / "c_fd"
    src.mkdir()
    names = ["d"] * 500
    make_chain(src, names, files={500: {"f.txt": b"x"}})
    dst = base / "c_dst"
    dst.mkdir()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
    try:
        _, completed, failed, _ = run_worker(src, dst)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    check(completed and not failed, "上限 64 でも 500 階層のコピーが完了した")


def test_deep_hardlink(base):
    print("\n--- D. 1 階層目と 2000 階層目のハードリンク ---")
    src = base / "d_hl"
    src.mkdir()
    names = ["d"] * 2000
    make_chain(src, names, files={1: {"a_first.bin": b"shared-data" * 100}})
    # 2000 階層目に、1 階層目のファイルへのハードリンクを作る
    fd1 = os.open(src / "d", DIR_FLAGS)
    fd = os.dup(fd1)
    for _ in range(1999):
        child = os.open("d", DIR_FLAGS, dir_fd=fd)
        os.close(fd)
        fd = child
    os.link("a_first.bin", "z_link.bin", src_dir_fd=fd1, dst_dir_fd=fd)
    os.close(fd)
    os.close(fd1)
    dst = base / "d_dst"
    dst.mkdir()
    _, completed, failed, logs = run_worker(src, dst)
    check(completed and not failed, "コピーが完了した")
    if completed:
        levels = read_chain(dst / "d_hl", names)
        first = levels[0][2].get("a_first.bin")
        link = levels[1999][2].get("z_link.bin")
        check(first and link and first[1] == link[1],
              "コピー先でも同じ実体（ハードリンク）になっている")
        check(any("ハードリンクを再現" in m for m in logs), "ハードリンクとして処理された")


def test_deep_readonly(base):
    print("\n--- E. 深い階層の読み取り専用フォルダー ---")
    src = base / "e_ro"
    src.mkdir()
    names = ["d"] * 1500
    make_chain(src, names, files={1500: {"f.txt": b"x"}},
               modes={1500: 0o500, 1000: 0o555})
    dst = base / "e_dst"
    dst.mkdir()
    _, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "読み取り専用フォルダーを含む 1500 階層をコピーできた")
    if completed:
        levels = read_chain(dst / "e_ro", names)
        check(levels[1499][0] == 0o500 and levels[999][0] == 0o555,
              "読み取り専用の権限（0500・0555）が設定された")

    print("  （公開の直前で失敗させる）")
    dst2 = base / "e_dst2"
    dst2.mkdir()
    original = main.publish_without_overwrite

    def fail_publish(*args):
        raise OSError(5, "Input/output error (simulated)")

    main.publish_without_overwrite = fail_publish
    try:
        _, completed, failed, logs = run_worker(src, dst2)
    finally:
        main.publish_without_overwrite = original
    check(failed and not completed, "エラーで停止した")
    check(not staging_left(dst2), "読み取り専用フォルダーを含む深い一時コピー領域が残っていない")
    check(not any("清掃に失敗" in m for m in logs), "清掃失敗の警告が出ていない")
    make_tree_removable(src)


def test_deep_cancel(base):
    print("\n--- F. 深い階層で中止 ---")
    src = base / "f_cancel"
    src.mkdir()
    names = ["d"] * 2000
    make_chain(src, names, files={1800: {"stop_here.txt": b"x"}})
    dst = base / "f_dst"
    dst.mkdir()

    def cancel_at_file(worker):
        original = worker._copy_file

        def wrapped(s, d):
            if Path(str(s)).name == "stop_here.txt":
                worker.cancel()
            return original(s, d)

        worker._copy_file = wrapped

    _, completed, failed, logs = run_worker(src, dst, cancel_at_file)
    check(failed and not completed and any("中止" in m for m in failed),
          "1800 階層目で中止された")
    check(not staging_left(dst), "一時コピー領域が残っていない")


def test_deep_stale(base):
    print("\n--- G. 残った深い一時コピー領域の削除 ---")
    dst = base / "g_dst"
    dst.mkdir()
    stale = dst / (main.STAGING_PREFIX + "deep")
    stale.mkdir()
    # 最下層（3000）は読み取り不可。大きさを数えるファイルはその1つ上に置く
    # （読み取り不可のフォルダーの中は、一般ユーザーには数えられないため）。
    make_chain(stale, ["d"] * 3000, files={2999: {"f": b"x" * 1000}, 3000: {"g": b"y"}},
               modes={3000: 0o000, 2000: 0o500})
    old = 1_000_000_000
    os.utime(stale, (old, old))
    found = main.find_stale_staging(dst)
    check([n for n, _ in found] == [stale.name], "深い一時コピー領域を検出した")
    check(found and found[0][1] >= 1000, "中身の大きさを数えられた")
    errors = main.remove_stale_staging(dst, [n for n, _ in found])
    check(not errors and not stale.exists(), "読み取り不可のフォルダーを含めて削除できた")


def test_moved_during_copy(base):
    print("\n--- H. コピー中に途中のフォルダーが別の場所へ移動された ---")
    src = base / "h_move"
    src.mkdir()
    names = ["a", "b", "c", "d"]
    make_chain(src, names, files={4: {"z.txt": b"x"}})
    elsewhere = base / "h_elsewhere"
    elsewhere.mkdir()
    dst = base / "h_dst"
    dst.mkdir()
    moved = []

    def move_while_inside(worker):
        original = worker._copy_file

        def wrapped(s, d):
            if Path(str(s)).name == "z.txt" and not moved:
                # c フォルダー（と中の d）を、b の外へ移動する
                os.rename(src / "a" / "b" / "c", elsewhere / "c")
                moved.append(True)
            return original(s, d)

        worker._copy_file = wrapped

    _, completed, failed, _ = run_worker(src, dst, move_while_inside)
    check(moved, "移動操作が実行された")
    check(failed and not completed, "コピーはエラーで停止した")
    check(any("別の場所へ移動されました" in m for m in failed), "移動されたことが報告された")
    check(not (dst / "h_move").exists() and not staging_left(dst),
          "何も公開されず、一時コピー領域も残っていない")


def main_test():
    print("=" * 72)
    print("UltraCopy 非常に深いフォルダー階層の確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="deep_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    try:
        test_very_deep(base)
        test_long_names(base)
        test_fd_limit(base)
        test_deep_hardlink(base)
        test_deep_readonly(base)
        test_deep_cancel(base)
        test_deep_stale(base)
        test_moved_during_copy(base)
    finally:
        make_tree_removable(base)
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
