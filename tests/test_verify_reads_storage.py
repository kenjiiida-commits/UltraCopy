"""レビュー指摘 7：検証がディスクではなくキャッシュを読んでいる可能性への対策。

A. O_DIRECT で読み直したとき、ページキャッシュを経由していない
   （読み直しの前後でキャッシュに載っているページ数が 0 のまま）
B. O_DIRECT 非対応のファイルシステムでは、キャッシュを捨てたことを確認してから読む
C. キャッシュを捨てたことを確認できなければ、検証結果と完了メッセージに明示する
D. 所有者に読み取り権限がないファイル（例: 0204）も、検証・コピーできる
E. 【root・ループデバイスが必要】キャッシュ上は正しく、ディスク上だけ壊れている
   コピー先を、検証で検出できる
"""
from pathlib import Path
import errno
import fcntl
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []


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


def write_synced(path, data):
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def resident(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return main._resident_pages(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


def drop_cache(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


class NoDirectIO:
    """O_DIRECT 非対応のファイルシステムを再現する（O_DIRECT 付きの open が EINVAL）。"""

    def __enter__(self):
        self.original = main.os.open

        def patched(path, flags, *args, **kwargs):
            if flags & getattr(os, "O_DIRECT", 0):
                raise OSError(errno.EINVAL, "Invalid argument (simulated)")
            return self.original(path, flags, *args, **kwargs)

        main.os.open = patched
        return self

    def __exit__(self, *exc):
        main.os.open = self.original
        return False


def test_direct_bypasses_cache(base):
    print("\n--- A. O_DIRECT の読み直しはキャッシュを経由しない ---")
    path = base / "a.bin"
    write_synced(path, os.urandom(8 * 1024 * 1024 + 123))
    drop_cache(path)
    before = resident(path)
    if before is None:
        print("[INFO] mincore が使えない環境のため省略します。")
        return
    check(before == 0, f"読み直し前はキャッシュに載っていない（{before} ページ）")
    digest, method = main.sha256_file_from_storage(path, lambda: None)
    if method != main.READ_DIRECT:
        print(f"[INFO] O_DIRECT 非対応のファイルシステムのため省略します（{method}）。")
        return
    after = resident(path)
    check(after == 0, f"読み直し後もキャッシュに載っていない（{after} ページ）")
    import hashlib
    check(digest == hashlib.sha256(path.read_bytes()).hexdigest(), "ハッシュ値が正しい")


def test_fallback_drops_cache(base):
    print("\n--- B. O_DIRECT 非対応ならキャッシュを捨ててから読む ---")
    path = base / "b.bin"
    write_synced(path, os.urandom(3 * 1024 * 1024))
    path.read_bytes()  # わざとキャッシュに載せる
    loaded = resident(path)
    if loaded is None:
        print("[INFO] mincore が使えない環境のため省略します。")
        return
    check(loaded > 0, f"読み直し前はキャッシュに載っている（{loaded} ページ）")
    with NoDirectIO():
        _, method = main.sha256_file_from_storage(path, lambda: None)
    check(method == main.READ_CACHE_DROPPED, f"キャッシュを捨てたことを確認して読んだ（{method}）")

    src = base / "b_src"
    src.mkdir()
    write_synced(src / "f.bin", os.urandom(100_000))
    dst = base / "b_dst"
    dst.mkdir()
    with NoDirectIO():
        worker, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "コピーが完了した")
    check(worker._verification_result == main.VERIFIED_FROM_STORAGE,
          f"検証結果は「{worker._verification_result}」")


def test_cache_possible_reported(base):
    print("\n--- C. キャッシュを捨てたことを確認できない場合 ---")
    src = base / "c_src"
    src.mkdir()
    for i in range(3):
        write_synced(src / f"f{i}.bin", os.urandom(50_000))
    dst = base / "c_dst"
    dst.mkdir()
    original = main._resident_pages
    main._resident_pages = lambda fd, size: None
    try:
        with NoDirectIO():
            worker, completed, failed, logs = run_worker(src, dst)
    finally:
        main._resident_pages = original
    message = completed[0] if completed else ""
    check(completed and not failed, "コピーは完了した")
    check(worker._verification_result == main.VERIFIED_MAYBE_CACHE,
          f"検証結果は「{worker._verification_result}」")
    check("3 件は、ディスクではなくキャッシュを読んだ可能性" in message,
          "完了メッセージに件数つきで明示された")
    check(sum("キャッシュを読んだ可能性" in m and "警告" in m for m in logs) == 1,
          "警告は1回だけ出た")


def test_owner_unreadable(base):
    print("\n--- D. 所有者に読み取り権限がないファイル（0204） ---")
    if os.geteuid() == 0:
        print("[INFO] root は権限に関係なく読めるため、一般ユーザーで実行してください。")
    src = base / "d_src"
    src.mkdir()
    f = src / "f.txt"
    write_synced(f, b"secret-ish" * 100)
    # 所有者は書き込みのみ、その他は読み取り可。一般ユーザーなら所有者は自分のため、
    # コピー元が読めるように「その他」の読み取り権限で読む状況は再現できない。
    # そこで、コピー先にだけこの権限が付く状況を作る（copystat の対象を 0204 にする）。
    original_copystat = main.shutil.copystat

    def copystat_then_204(s, d, *args, **kwargs):
        original_copystat(s, d, *args, **kwargs)
        if Path(str(s)).name == "f.txt":
            os.chmod(d, 0o204)

    main.shutil.copystat = copystat_then_204
    dst = base / "d_dst"
    dst.mkdir()
    try:
        _, completed, failed, _ = run_worker(src, dst)
    finally:
        main.shutil.copystat = original_copystat
    check(completed and not failed, "コピーと検証が完了した")
    out = dst / "d_src" / "f.txt"
    if out.exists():
        check(stat.S_IMODE(out.stat().st_mode) == 0o204, "権限 0204 が保たれた")


FS_IOC_FIEMAP = 0xC020660B


def physical_extents(path):
    """FIEMAP でファイルのデータがデバイス上のどこにあるかを調べる。"""
    count = 32
    header = struct.pack("=QQLLLL", 0, 0xFFFFFFFFFFFFFFFF, 1, 0, count, 0)
    buf = bytearray(header + b"\0" * (56 * count))
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.ioctl(fd, FS_IOC_FIEMAP, buf)
    finally:
        os.close(fd)
    mapped = struct.unpack_from("=L", buf, 20)[0]
    extents = []
    for i in range(mapped):
        logical, physical, length = struct.unpack_from("=QQQ", buf, 32 + 56 * i)
        extents.append((logical, physical, length))
    return extents


def test_corruption_behind_cache(base):
    print("\n--- E. ディスク上だけ壊れたコピー先を検出できる（ループデバイス使用） ---")
    if os.geteuid() != 0 or not Path("/dev/loop-control").exists():
        print("[INFO] root とループデバイスが必要なため省略します。")
        return
    work = Path(tempfile.mkdtemp(prefix="ultracopy_loop_"))
    image = work / "disk.img"
    mount = work / "mnt"
    mount.mkdir()
    try:
        subprocess.run(["truncate", "-s", "64M", str(image)], check=True)
        subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True)
        if subprocess.run(["mount", "-o", "loop", str(image), str(mount)]).returncode:
            print("[INFO] ループデバイスをマウントできないため省略します。")
            return

        src = base / "e_src"
        src.mkdir()
        write_synced(src / "data.bin", b"GOOD" * 256 * 1024)  # 1 MiB
        corrupted = []

        def corrupt_after_fsync(worker):
            original = worker._fsync_file

            def wrapped(path):
                result = original(path)
                if Path(str(path)).name == "data.bin" and not corrupted:
                    # ファイルシステムを通さず、イメージ上の該当ブロックを直接壊す。
                    # マウント中のファイルシステムのキャッシュは正しい内容のまま。
                    with open(image, "r+b") as disk:
                        for _, physical, length in physical_extents(path):
                            disk.seek(physical)
                            disk.write(b"BAD!" * (length // 4))
                        disk.flush()
                        os.fsync(disk.fileno())
                    with open(path, "rb") as cached:
                        corrupted.append(cached.read(4))
                return result

            worker._fsync_file = wrapped

        _, completed, failed, _ = run_worker(src, mount, corrupt_after_fsync)
        check(bool(corrupted), "コピー先のディスク上のデータを壊した")
        check(corrupted and corrupted[0] == b"GOOD",
              "通常の読み込み（キャッシュ）では壊れていないように見える")
        check(failed and not completed, "コピーはエラーで停止した")
        check(any("SHA-256検証不一致" in m for m in failed),
              "検証がディスク上の破損を検出した（SHA-256検証不一致）")
        check(not (mount / "e_src").exists(), "壊れたコピーは公開されていない")
    finally:
        subprocess.run(["umount", str(mount)], stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 7（検証がキャッシュを読む問題）確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="verify_storage_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    try:
        test_direct_bypasses_cache(base)
        test_fallback_drops_cache(base)
        test_cache_possible_reported(base)
        test_owner_unreadable(base)
        test_corruption_behind_cache(base)
    finally:
        for root, dirs, files in os.walk(base):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), 0o644)
                except OSError:
                    pass
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
