"""レビュー指摘 10：renameat2 の非対応ファイルシステム（EINVAL）と ARM などの扱い。

A. renameat2 が EINVAL（RENAME_NOREPLACE 非対応のファイルシステム）の場合
   - ファイルはハードリンク方式、フォルダーは名前の予約方式で、上書きせずに公開する
   - 同じ結果になるシステムコールは呼ばない
   - ハードリンクも作れない場合（FAT・一部の FUSE）は、ファイルも予約方式で公開する
   - 公開先に同名の項目があれば、どの方式でも上書きしない
B. システムコール番号の表
   - x86_64=316、aarch64=276、32ビット ARM=382 など
   - 64ビットカーネル上の 32ビット版 Python では、32ビット用の番号を使う
   - 分からない CPU ではシステムコールを呼ばない
C. CopyWorker を通したフォルダーのコピーでも、代わりの方式で公開され、ログに残る
"""
from pathlib import Path
import ctypes
import errno
import os
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


class FakeFunction:
    def __init__(self, error, calls):
        self.error = error
        self.calls = calls
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        ctypes.set_errno(self.error)
        return -1


class FakeLibc:
    """renameat2 は指定の errno で失敗し、syscall の呼び出しは記録する。"""

    def __init__(self, renameat2_error=None, syscall_error=errno.ENOSYS):
        self.renameat2_calls = []
        self.syscall_calls = []
        if renameat2_error is not None:
            self.renameat2 = FakeFunction(renameat2_error, self.renameat2_calls)
        self.syscall = FakeFunction(syscall_error, self.syscall_calls)


class NoLink:
    def __enter__(self):
        self.original = main.os.link

        def failing(*args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted (simulated FAT)")

        main.os.link = failing
        return self

    def __exit__(self, *exc):
        main.os.link = self.original
        return False


def publish(libc, kind, existing=None, no_link=False):
    """戻り値: (例外, 公開先の状態, ステージング側が残っているか, ログ)"""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        staging = root / "staging"
        destination = root / "destination"
        staging.mkdir()
        destination.mkdir()
        staged = staging / "item"
        target = destination / "item"
        if kind == "dir":
            staged.mkdir()
            (staged / "inside.txt").write_text("new")
        else:
            staged.write_text("new")
        if existing == "file":
            target.write_text("keep")
        elif existing == "dir":
            target.mkdir()
            (target / "keep.txt").write_text("keep")

        logs = []
        staged_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        error = None
        try:
            with patch.object(main.ctypes, "CDLL", return_value=libc):
                if no_link:
                    with NoLink():
                        main.publish_without_overwrite(
                            staged, target, staged_fd, target_fd, logs.append)
                else:
                    main.publish_without_overwrite(
                        staged, target, staged_fd, target_fd, logs.append)
        except Exception as exc:
            error = exc
        finally:
            os.close(staged_fd)
            os.close(target_fd)

        if target.is_dir():
            state = {p.name: p.read_text() for p in target.iterdir()}
        elif target.exists():
            state = target.read_text()
        else:
            state = None
        return error, state, staged.exists(), logs


def test_einval():
    print("\n--- A. renameat2 が EINVAL（RENAME_NOREPLACE 非対応） ---")
    libc = FakeLibc(renameat2_error=errno.EINVAL)
    error, state, left, logs = publish(libc, "file")
    check(error is None and state == "new" and not left, "ファイルを公開した")
    check(any("ハードリンク方式" in m for m in logs), "ハードリンク方式であることを記録した")
    check(not libc.syscall_calls, "同じ結果になるシステムコールは呼ばなかった")

    libc = FakeLibc(renameat2_error=errno.EINVAL)
    error, state, left, logs = publish(libc, "dir")
    check(error is None and state == {"inside.txt": "new"} and not left,
          "フォルダーを公開した")
    check(any("名前を予約する方式" in m for m in logs), "予約方式であることを記録した")

    libc = FakeLibc(renameat2_error=errno.EINVAL)
    error, state, left, logs = publish(libc, "file", no_link=True)
    check(error is None and state == "new" and not left,
          "ハードリンクも作れない場合、ファイルを予約方式で公開した")

    for kind, existing, no_link in (
        ("file", "file", False), ("file", "file", True),
        ("dir", "dir", False), ("dir", "file", False), ("file", "dir", True),
    ):
        libc = FakeLibc(renameat2_error=errno.EINVAL)
        error, state, left, _ = publish(libc, kind, existing, no_link)
        expected = "keep" if existing == "file" else {"keep.txt": "keep"}
        check(isinstance(error, FileExistsError) and state == expected and left,
              f"{kind} → 既存の{existing}を上書きしなかった"
              f"{'（ハードリンク不可）' if no_link else ''}")


def test_syscall_numbers():
    print("\n--- B. システムコール番号 ---")
    n = main.renameat2_syscall_number
    check(n("x86_64", 64) == 316, "x86_64 は 316")
    check(n("aarch64", 64) == 276, "aarch64（Raspberry Pi 4/5 の 64ビット OS など）は 276")
    check(n("armv7l", 32) == 382, "32ビット ARM は 382")
    check(n("aarch64", 32) == 382, "64ビットカーネル上の 32ビット ARM 版 Python は 382")
    check(n("x86_64", 32) == 353, "64ビットカーネル上の 32ビット x86 版 Python は 353")
    check(n("riscv64", 64) == 276, "riscv64 は 276")
    check(n("mips", 32) is None, "分からない CPU は None")

    # glibc に renameat2 がない環境（古い glibc）を、aarch64 として再現する。
    uname = os.uname_result(("Linux", "pi", "6.6", "#1", "aarch64"))
    libc = FakeLibc(renameat2_error=None, syscall_error=errno.ENOSYS)
    with patch.object(main.os, "uname", return_value=uname):
        error, state, left, _ = publish(libc, "file")
    expected = 276 if ctypes.sizeof(ctypes.c_void_p) == 8 else 382
    check(libc.syscall_calls and libc.syscall_calls[0][0].value == expected,
          f"aarch64 ではシステムコール {expected} を呼んだ")
    check(error is None and state == "new", "ENOSYS の後、ハードリンク方式で公開した")

    uname = os.uname_result(("Linux", "h", "6.6", "#1", "mips"))
    libc = FakeLibc(renameat2_error=None)
    with patch.object(main.os, "uname", return_value=uname):
        error, state, _, logs = publish(libc, "dir")
    check(not libc.syscall_calls, "分からない CPU ではシステムコールを呼ばなかった")
    check(error is None and state == {"inside.txt": "new"},
          "分からない CPU でもフォルダーを予約方式で公開した")


def test_worker_integration():
    print("\n--- C. CopyWorker を通したフォルダーのコピー ---")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        main.DATA_DIR = root / "data"
        main.DB_PATH = main.DATA_DIR / "history.sqlite3"
        src = root / "photos"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("b")
        dst = root / "dst"
        dst.mkdir()
        worker = main.CopyWorker(src, dst, True)
        completed, failed, logs = [], [], []
        worker.completed.connect(completed.append)
        worker.failed.connect(failed.append)
        worker.log.connect(logs.append)
        with patch.object(main.ctypes, "CDLL",
                          return_value=FakeLibc(renameat2_error=errno.EINVAL)):
            worker.run()
        check(completed and not failed, "コピーが完了した")
        check((dst / "photos" / "sub" / "b.txt").read_text() == "b", "内容が正しい")
        check(any("名前を予約する方式" in m for m in logs), "ログに公開方式が残った")


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 10（renameat2 の EINVAL・CPU ごとの扱い）確認テスト")
    print("=" * 72)
    test_einval()
    test_syscall_numbers()
    test_worker_integration()
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
