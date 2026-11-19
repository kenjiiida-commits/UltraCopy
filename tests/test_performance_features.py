"""性能改善の確認テスト。

A. スパースファイル（穴のあるファイル）は、コピー先でも穴のまま（容量を食わない）
   - 途中に穴、末尾が穴、全体が穴、の各パターン
   - copy_file_range が使えない通常コピーの経路でも同じ
   - SEEK_DATA 非対応のファイルシステムでも、内容は正しくコピーされる
B. 進捗表示の更新は、ファイル数が多くても一定間隔に抑えられる
C. 空き容量が明らかに足りない場合は、コピー前にエラーにする
   - スパースファイルやハードリンクで、必要な容量を多く見積もりすぎない
"""
from pathlib import Path
import errno
import hashlib
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []
MiB = 1024 * 1024


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


def run_worker(source, destination_parent, verify=True):
    worker = main.CopyWorker(source, destination_parent, verify)
    completed, failed, logs, progress = [], [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    worker.progress.connect(lambda *a: progress.append(a))
    started = time.monotonic()
    worker.run()
    return worker, completed, failed, logs, progress, time.monotonic() - started


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def allocated(path):
    return os.stat(path).st_blocks * 512


def make_sparse(path, size, regions):
    with open(path, "wb") as f:
        f.truncate(size)
        for offset, length in regions:
            f.seek(offset)
            f.write(os.urandom(length))


def supports_holes(directory):
    probe = Path(directory) / ".hole_probe"
    make_sparse(probe, 16 * MiB, [])
    result = allocated(probe) < MiB
    probe.unlink()
    return result


def test_sparse(base):
    print("\n--- A. スパースファイル ---")
    if not supports_holes(base):
        print("[INFO] このファイルシステムは穴に対応していないため省略します。")
        return
    src = base / "a_src"
    src.mkdir()
    make_sparse(src / "middle_holes.img", 64 * MiB, [(0, MiB), (40 * MiB, 2 * MiB)])
    make_sparse(src / "trailing_hole.img", 32 * MiB, [(0, 3 * MiB)])
    make_sparse(src / "all_hole.img", 32 * MiB, [])
    (src / "normal.txt").write_bytes(os.urandom(100_000))

    for label, cfr_patch in (("copy_file_range", None), ("通常コピー", errno.ENOSYS)):
        dst = base / f"a_dst_{len(label)}"
        dst.mkdir()
        if cfr_patch:
            def failing(*args):
                raise OSError(cfr_patch, "simulated")
            with patch.object(main.os, "copy_file_range", failing):
                _, completed, failed, *_ = run_worker(src, dst)
        else:
            _, completed, failed, *_ = run_worker(src, dst)
        check(completed and not failed, f"[{label}] コピーが完了した")
        for name in ("middle_holes.img", "trailing_hole.img", "all_hole.img", "normal.txt"):
            s, d = src / name, dst / "a_src" / name
            same = d.exists() and s.stat().st_size == d.stat().st_size and sha(s) == sha(d)
            check(same, f"[{label}] {name}: サイズと内容が一致")
            if name.endswith(".img") and d.exists():
                check(allocated(d) <= allocated(s) + MiB,
                      f"[{label}] {name}: 穴のまま（使用容量 "
                      f"{allocated(d) // MiB} MiB / 見かけ {d.stat().st_size // MiB} MiB）")

    print("  SEEK_DATA 非対応のファイルシステムを再現")
    dst = base / "a_dst_noseek"
    dst.mkdir()
    real_lseek = os.lseek

    def lseek_without_seek_data(fd, offset, whence):
        if whence in (os.SEEK_DATA, os.SEEK_HOLE):
            raise OSError(errno.EINVAL, "simulated")
        return real_lseek(fd, offset, whence)

    with patch.object(main.os, "lseek", lseek_without_seek_data):
        _, completed, failed, *_ = run_worker(src, dst)
    check(completed and not failed
          and all(sha(src / n) == sha(dst / "a_src" / n)
                  for n in ("middle_holes.img", "trailing_hole.img", "all_hole.img")),
          "SEEK_DATA 非対応でも内容は正しくコピーされた")


def test_progress_throttle(base):
    print("\n--- B. 進捗表示の更新回数 ---")
    src = base / "b_src"
    src.mkdir()
    for i in range(1500):
        (src / f"f{i:04d}.txt").write_bytes(b"x" * 100)
    dst = base / "b_dst"
    dst.mkdir()
    _, completed, failed, _, progress, elapsed = run_worker(src, dst, verify=False)
    limit = int(elapsed / main.CopyWorker.PROGRESS_INTERVAL) + 3
    check(completed and not failed, "コピーが完了した")
    check(len(progress) <= limit,
          f"1500 ファイルで進捗の更新は {len(progress)} 回（上限 {limit} 回、"
          f"所要 {elapsed:.2f} 秒）")
    check(progress and progress[-1][0] == progress[-1][1] and progress[-1][3] == "完了",
          "最後の進捗は 100%・完了")


class FakeStatvfs:
    def __init__(self, available):
        self.f_bavail = available // 4096
        self.f_frsize = 4096


def test_free_space(base):
    print("\n--- C. 空き容量の事前確認 ---")
    src = base / "c_src"
    src.mkdir()
    (src / "data.bin").write_bytes(os.urandom(8 * MiB))
    dst = base / "c_dst"
    dst.mkdir()
    with patch.object(main.os, "statvfs", return_value=FakeStatvfs(4 * MiB)):
        worker, completed, failed, *_ = run_worker(src, dst)
    check(failed and not completed, "空き 4 MiB に 8 MiB はコピーせずエラーにした")
    check(any("空き容量が足りません" in m and "必要" in m for m in failed),
          "必要な容量と空き容量を表示した")
    check(worker.done_bytes == 0 and not list(dst.iterdir()),
          "何も書き込まず、一時コピー領域も作らなかった")

    src2 = base / "c_src2"
    src2.mkdir()
    (src2 / "a.bin").write_bytes(os.urandom(6 * MiB))
    os.link(src2 / "a.bin", src2 / "b.bin")
    if supports_holes(base):
        make_sparse(src2 / "sparse.img", 512 * MiB, [(0, MiB)])
    dst2 = base / "c_dst2"
    dst2.mkdir()
    with patch.object(main.os, "statvfs", return_value=FakeStatvfs(10 * MiB)):
        _, completed, failed, *_ = run_worker(src2, dst2)
    check(completed and not failed,
          "ハードリンク（6 MiB×2 名）と見かけ 512 MiB のスパースファイルは、"
          "空き 10 MiB でもコピーできると判断した")


def main_test():
    print("=" * 72)
    print("UltraCopy 性能改善の確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="performance_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    try:
        test_sparse(base)
        test_progress_throttle(base)
        test_free_space(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
