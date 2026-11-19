from __future__ import annotations

import csv
import ctypes
import errno
import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QProgressBar,
    QTextEdit, QMessageBox, QGroupBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog,
    QDialogButtonBox, QComboBox,
)

APP_VERSION = "0.7.8"
CHUNK_SIZE = 4 *  1024 * 1024
DATA_DIR = Path.home() / ".local" / "share" / "ultracopy"
DB_PATH = DATA_DIR / "history.sqlite3"

STAGING_PREFIX = ".ultracopy-staging-"
# 作成直後（ロック取得前）の一時領域を誤って消さないための猶予（秒）
STALE_STAGING_MIN_AGE = 60

_DIR_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


# fsync をこのエラーで拒否するファイルシステムでは、書き込み確定を保証できない。
# （データ自体は書き込まれているため、警告を出して処理は続ける）
FSYNC_UNSUPPORTED_ERRORS = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}

# syncfs が書き込みエラーを正しく報告するのは Linux 5.8 以降。
SYNCFS_MIN_KERNEL = (5, 8)


def kernel_version() -> tuple[int, int]:
    """実行中の Linux カーネルの (メジャー, マイナー) を返す。"""
    try:
        parts = os.uname().release.split(".")
        return int(parts[0]), int("".join(c for c in parts[1] if c.isdigit()) or 0)
    except (ValueError, IndexError):
        return (0, 0)


def syncfs(fd: int) -> None:
    """fd があるファイルシステム全体の書き込みをディスクに確定させる。"""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "syncfs", None)
    if function is None:
        raise OSError(errno.ENOSYS, "syncfs を利用できません")
    function.argtypes = [ctypes.c_int]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(fd) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def syncfs_available() -> bool:
    if kernel_version() < SYNCFS_MIN_KERNEL:
        return False
    try:
        return getattr(ctypes.CDLL(None), "syncfs", None) is not None
    except OSError:
        return False


RESULT_SUCCESS = "成功"
RESULT_SUCCESS_WITH_SKIPS = "成功（スキップあり）"
# 完了メッセージに並べる件数の上限（履歴には SKIP_HISTORY_LIMIT 件まで記録）
SKIP_MESSAGE_LIMIT = 10
SKIP_HISTORY_LIMIT = 1000


def special_file_kind(mode: int) -> str:
    """コピーしない特殊ファイルの種類を、利用者向けの言葉で返す。"""
    if stat.S_ISFIFO(mode):
        return "名前付きパイプ（FIFO）"
    if stat.S_ISSOCK(mode):
        return "ソケット"
    if stat.S_ISCHR(mode):
        return "キャラクターデバイス"
    if stat.S_ISBLK(mode):
        return "ブロックデバイス"
    return "不明な種類"


def format_skipped(skipped: list[tuple[str, str]], limit: int) -> str:
    """スキップした項目の一覧を文字列にする。limit を超えた分は件数だけ示す。"""
    lines = [f"・{kind}: {path}" for path, kind in skipped[:limit]]
    if len(skipped) > limit:
        lines.append(f"・ほか {len(skipped) - limit:,} 件")
    return "\n".join(lines)


def walk_directories(parent_fd: int, name: str, visit, leave=None,
                      prepare=None, ignore_errors: bool = False) -> None:
    """parent_fd 内のフォルダー name 以下を、再帰を使わずにたどる。

    - visit(fd): フォルダーを開いた直後に呼ばれ、たどる子フォルダー名のリストを返す
    - leave(fd, parent_fd, name): 中身をすべてたどった後に呼ばれる（深い階層から順）
    - prepare(parent_fd, name): フォルダーを開く前に呼ばれる
    - ignore_errors: 開けない子フォルダーを飛ばす（容量の見積もりなど）

    同時に開いているフォルダーは最大2個で、階層の深さに関係しない。
    親に戻るときは「..」で戻り、元の親と同じフォルダーであることを確かめる。
    パスの長さの上限（4096 バイト）にも影響されない。
    """
    if prepare:
        prepare(parent_fd, name)
    fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    stack = []
    try:
        st = os.fstat(fd)
        stack.append([name, (st.st_dev, st.st_ino), list(visit(fd)), 0])
        while stack:
            frame = stack[-1]
            if frame[3] < len(frame[2]):
                child = frame[2][frame[3]]
                frame[3] += 1
                try:
                    if prepare:
                        prepare(fd, child)
                    child_fd = os.open(child, _DIR_OPEN_FLAGS, dir_fd=fd)
                except OSError:
                    if ignore_errors:
                        continue
                    raise
                os.close(fd)
                fd = child_fd
                st = os.fstat(fd)
                stack.append([child, (st.st_dev, st.st_ino), list(visit(fd)), 0])
                continue

            stack.pop()
            up = None
            if stack:
                up = os.open("..", _DIR_OPEN_FLAGS, dir_fd=fd)
                up_stat = os.fstat(up)
                if (up_stat.st_dev, up_stat.st_ino) != stack[-1][1]:
                    os.close(up)
                    raise OSError(
                        errno.ESTALE,
                        "たどっている途中でフォルダーが別の場所へ移動されました",
                        frame[0],
                    )
            try:
                if leave:
                    leave(fd, up if up is not None else parent_fd, frame[0])
            finally:
                os.close(fd)
                fd = up
    finally:
        if fd is not None:
            os.close(fd)


def remove_tree_at(parent_fd: int, name: str) -> None:
    """parent_fd 内の name を、シンボリックリンクを辿らずに削除する。

    shutil.rmtree(dir_fd=...) は Python 3.11 以降でしか使えないため自前で実装する。
    読み取り専用（0555 や 0000）のフォルダーも、所有者として書き込み権限を
    付け直してから中身を削除する。階層がどれだけ深くても動作する。
    """
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    def prepare(pfd, child):
        child_stat = os.stat(child, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISDIR(child_stat.st_mode):
            raise OSError(errno.ENOTDIR, "削除中にフォルダーが置き換えられました", child)
        mode = stat.S_IMODE(child_stat.st_mode)
        if mode & 0o700 != 0o700:
            os.chmod(child, mode | 0o700, dir_fd=pfd)

    def visit(fd):
        directories = []
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                directories.append(entry.name)
            else:
                os.unlink(entry.name, dir_fd=fd)
        return directories

    def leave(fd, pfd, child):
        os.rmdir(child, dir_fd=pfd)

    walk_directories(parent_fd, name, visit, leave, prepare)


_IGNORED_XATTR_ERRORS = (errno.EPERM, errno.ENOTSUP, errno.ENODATA, errno.EINVAL)


class AnchoredPath(type(Path())):
    """フォルダーの FD に固定されたコピー元のパス。

    表示・比較（str() や ==）では元のパスと同じに振る舞うが、OS の関数に
    渡されるときは /proc/self/fd/<フォルダーのFD>/<名前> になる。
    このためパスの途中のフォルダーが差し替えられても、最初に開いた
    フォルダーの中だけを参照する（openat 相当）。名前自体の差し替えは、
    呼び出し側の O_NOFOLLOW や follow_symlinks=False で防ぐ。
    """

    @classmethod
    def at(cls, display_path, dir_fd: int) -> "AnchoredPath":
        anchored = cls(display_path)
        anchored._anchor_fd = dir_fd
        return anchored

    def __fspath__(self):
        anchor_fd = getattr(self, "_anchor_fd", None)
        if anchor_fd is None:
            # 派生パス（親・子）は固定されていないため、OS に渡してはならない。
            raise RuntimeError(f"FDに固定されていないコピー元パスです: {self}")
        return f"/proc/self/fd/{anchor_fd}/{self.name}"


class DirectoryMetadata:
    """コピー元フォルダーの属性（日時・拡張属性・権限）を FD から読み取った控え。"""

    def __init__(self, dir_fd: int):
        st = os.fstat(dir_fd)
        if not stat.S_ISDIR(st.st_mode):
            raise IOError("コピー元フォルダーではありません")
        self.atime_ns = st.st_atime_ns
        self.mtime_ns = st.st_mtime_ns
        self.mode = stat.S_IMODE(st.st_mode)
        self.xattrs = []
        try:
            names = os.listxattr(dir_fd)
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.ENODATA, errno.EINVAL):
                raise
            names = []
        for name in names:
            try:
                self.xattrs.append((name, os.getxattr(dir_fd, name)))
            except OSError as exc:
                if exc.errno not in _IGNORED_XATTR_ERRORS:
                    raise

    def apply_to_fd(self, dst_fd: int) -> None:
        """shutil.copystat と同じ順序（日時→拡張属性→権限）で適用する。"""
        os.utime(dst_fd, ns=(self.atime_ns, self.mtime_ns))
        for name, value in self.xattrs:
            try:
                os.setxattr(dst_fd, name, value)
            except OSError as exc:
                if exc.errno not in _IGNORED_XATTR_ERRORS:
                    raise
        os.chmod(dst_fd, self.mode)


def find_stale_staging(destination_parent: Path) -> list[tuple[str, int]]:
    """異常終了で残った一時コピー領域を探す。

    使用中の一時領域はコピー処理がロック（flock）を保持しているため対象外。
    自分が所有するものだけを返す。戻り値は (名前, 合計バイト数) のリスト。
    """
    found = []
    now = time.time()
    try:
        parent_fd = os.open(destination_parent, _DIR_OPEN_FLAGS)
    except OSError:
        return found
    try:
        with os.scandir(parent_fd) as it:
            names = [
                e.name for e in it
                if e.name.startswith(STAGING_PREFIX)
                and e.is_dir(follow_symlinks=False)
            ]
        for name in names:
            try:
                st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                continue
            if st.st_uid != os.getuid():
                continue
            if now - st.st_mtime < STALE_STAGING_MIN_AGE:
                continue
            if not _staging_is_unlocked(parent_fd, name):
                continue
            found.append((name, _tree_size_at(parent_fd, name)))
    finally:
        os.close(parent_fd)
    return found


def _staging_is_unlocked(parent_fd: int, name: str) -> bool:
    try:
        fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError:
        # 0000 などで開けない場合は、ロックされていない（使用中ではない）。
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        return True
    finally:
        os.close(fd)  # ロックも同時に解放される
    return True


def _tree_size_at(parent_fd: int, name: str) -> int:
    total = 0

    def visit(fd):
        nonlocal total
        directories = []
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return directories

    try:
        walk_directories(parent_fd, name, visit, ignore_errors=True)
    except OSError:
        pass
    return total


def remove_stale_staging(destination_parent: Path, names: list[str]) -> list[str]:
    """find_stale_staging で見つけた一時領域を削除する。失敗したものの説明を返す。"""
    errors = []
    parent_fd = os.open(destination_parent, _DIR_OPEN_FLAGS)
    try:
        for name in names:
            if not name.startswith(STAGING_PREFIX) or "/" in name:
                continue
            # 削除直前にも、使用中でないことを確認する。
            if not _staging_is_unlocked(parent_fd, name):
                errors.append(f"{name}: 使用中のため削除しません")
                continue
            try:
                remove_tree_at(parent_fd, name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
    finally:
        os.close(parent_fd)
    return errors


def init_history_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS copy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                result TEXT NOT NULL,
                bytes_copied INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                verification TEXT NOT NULL,
                details TEXT NOT NULL
            )
        """)


def record_history(source: Path, destination: Path, result: str,
                   bytes_copied: int, elapsed: float,
                   verification: str, details: str) -> None:
    try:
        init_history_db()
        with sqlite3.connect(DB_PATH) as db:
            db.execute(
                """INSERT INTO copy_history
                   (started_at, source, destination, result, bytes_copied,
                    elapsed_seconds, verification, details)
                   VALUES (datetime('now','localtime'),?,?,?,?,?,?,?)""",
                (str(source), str(destination), result, bytes_copied,
                 elapsed, verification, details),
            )
    except Exception:
        # History failure must not turn a successful copy into a failed copy.
        pass


def scan_source(source: Path) -> tuple[int, int]:
    """Count regular files and bytes without following symbolic links."""
    count, total, _ = scan_source_detail(source)
    return count, total


def scan_source_detail(source: Path) -> tuple[int, int, int]:
    """(ファイル数, 合計サイズ, 必要な容量の下限) を返す。

    必要な容量の下限は、実際に使われているブロック（穴の部分は数えない）の合計で、
    ハードリンクされたファイルは1回だけ数える。
    """
    count = total = allocated = 0
    seen = set()

    def add(st):
        nonlocal count, total, allocated
        count += 1
        total += st.st_size
        key = (st.st_dev, st.st_ino)
        if st.st_nlink > 1:
            if key in seen:
                return
            seen.add(key)
        allocated += min(st.st_blocks * 512, st.st_size)

    try:
        st = os.stat(source, follow_symlinks=False)
    except OSError:
        return 0, 0, 0
    if stat.S_ISLNK(st.st_mode):
        return 0, 0, 0
    if stat.S_ISREG(st.st_mode):
        add(st)
        return count, total, allocated
    def visit(fd):
        directories = []
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    add(entry.stat(follow_symlinks=False))
            except OSError:
                continue
        return directories

    # 再帰を使わず、パスの長さにも影響されない方法でたどる。
    try:
        parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return count, total, allocated
    try:
        walk_directories(parent_fd, source.name, visit, ignore_errors=True)
    except OSError:
        pass
    finally:
        os.close(parent_fd)
    return count, total, allocated


def check_free_space(destination_parent: Path, required: int) -> None:
    """コピー先の空き容量が明らかに足りない場合は、コピー前にエラーにする。"""
    try:
        vfs = os.statvfs(destination_parent)
    except OSError:
        return
    available = vfs.f_bavail * vfs.f_frsize
    if required > available:
        raise OSError(
            errno.ENOSPC,
            "コピー先の空き容量が足りません"
            f"（必要: {format_bytes(required)} 以上 / 空き: {format_bytes(available)}）",
            str(destination_parent),
        )


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}"
        value /= 1024


def sha256_file(path: Path, checkpoint) -> str:
    digest = hashlib.sha256()
    # シンボリックリンクを辿らず、FIFO などでも待ち続けない開き方をする。
    fd = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IOError(f"通常ファイルではないためハッシュを計算できません: {path}")
        os.set_blocking(fd, True)
        stream = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        while True:
            checkpoint()
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# コピー先を読み直したときの方法
READ_DIRECT = "direct"            # O_DIRECT：キャッシュを通さずストレージから読んだ
READ_CACHE_DROPPED = "dropped"    # キャッシュを捨てたことを確認してから読んだ
READ_CACHE_POSSIBLE = "cache"     # キャッシュから読んだ可能性を否定できない

VERIFIED_FROM_STORAGE = "SHA-256検証済み"
VERIFIED_MAYBE_CACHE = "SHA-256検証済み（一部キャッシュ経由の可能性あり）"

_OWNER_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_own_file_readonly(path, extra_flags: int = 0) -> int:
    """自分が作ったコピー先ファイルを読み取り用に開く。

    コピー元の権限によっては、所有者に読み取り権限がない（例: 0204）。
    その場合は一時的に所有者の読み取り権限を付けて開き、すぐ元に戻す。
    （権限の確認は開く時点だけなので、開いた後は元の権限でも読める）
    非公開のステージング領域内のファイルにだけ使うこと。
    """
    try:
        fd = os.open(path, _OWNER_READ_FLAGS | extra_flags)
    except PermissionError:
        st = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise
        mode = stat.S_IMODE(st.st_mode)
        os.chmod(path, mode | stat.S_IRUSR)
        try:
            fd = os.open(path, _OWNER_READ_FLAGS | extra_flags)
        finally:
            os.chmod(path, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IOError(f"通常ファイルではありません: {path}")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _resident_pages(fd: int, size: int):
    """ファイルのうちページキャッシュに残っているページ数を返す（不明なら None）。"""
    if size == 0:
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.mmap.restype = ctypes.c_void_p
        libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_long]
        libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        page = os.sysconf("SC_PAGE_SIZE")
        window = 1024 * 1024 * 1024  # 1 GiB ずつ調べる
        map_failed = ctypes.c_void_p(-1).value
        resident = 0
        offset = 0
        while offset < size:
            length = min(window, size - offset)
            addr = libc.mmap(None, length, 0x1, 0x01, fd, offset)  # PROT_READ, MAP_SHARED
            if addr in (None, map_failed):
                return None
            try:
                vec = (ctypes.c_ubyte * ((length + page - 1) // page))()
                if libc.mincore(addr, length, vec) != 0:
                    return None
                resident += sum(b & 1 for b in vec)
            finally:
                libc.munmap(addr, length)
            offset += length
        return resident
    except (OSError, AttributeError, ValueError):
        return None


_direct_buffers = threading.local()


def _direct_io_buffer():
    """O_DIRECT 用のページ境界に揃ったバッファ（スレッドごとに1つを使い回す）。"""
    buffer = getattr(_direct_buffers, "buffer", None)
    if buffer is None:
        import mmap
        buffer = mmap.mmap(-1, CHUNK_SIZE)
        _direct_buffers.buffer = buffer
    return buffer


def sha256_file_from_storage(path, checkpoint) -> tuple[str, str]:
    """コピー先をページキャッシュではなくストレージから読み直してハッシュを計算する。

    1. O_DIRECT で読む（キャッシュを通らない）
    2. O_DIRECT 非対応なら、キャッシュを捨て（posix_fadvise DONTNEED）、
       mincore で本当に捨てられたことを確かめてから読む
    3. 確かめられなければ READ_CACHE_POSSIBLE として報告する
    戻り値は (ハッシュ値, 読み方)。
    （O_DIRECT の読み込みは、未書き込みのデータを先にディスクへ書き出してから読む。
      2 の方法では、捨てられるのは書き込み済みのキャッシュだけなので、先に
      fdatasync でこのファイルのデータを書き出す）
    """
    digest = hashlib.sha256()

    fd = None
    try:
        fd = open_own_file_readonly(path, getattr(os, "O_DIRECT", 0))
    except OSError as exc:
        if exc.errno != errno.EINVAL or not hasattr(os, "O_DIRECT"):
            raise

    if fd is not None:
        buffer = _direct_io_buffer()
        try:
            size = os.fstat(fd).st_size
            done = 0
            first = True
            # ファイルサイズ分を読んだら終わる（末尾の確認のためだけに
            # もう一度ディスクを読むと、小さいファイルでは時間が倍になる）。
            while done < size:
                checkpoint()
                try:
                    count = os.readv(fd, [buffer])
                except OSError as exc:
                    if first and exc.errno == errno.EINVAL:
                        break  # このファイルシステムは O_DIRECT 読み込み非対応
                    raise
                first = False
                if count == 0:
                    break  # 想定より短い（ステージング内のファイルでは通常起きない）
                digest.update(memoryview(buffer)[:count])
                done += count
            else:
                return digest.hexdigest(), READ_DIRECT
            if not first:
                return digest.hexdigest(), READ_DIRECT
        finally:
            os.close(fd)
        digest = hashlib.sha256()

    fd = open_own_file_readonly(path)
    try:
        size = os.fstat(fd).st_size
        try:
            os.fdatasync(fd)
        except OSError as exc:
            if exc.errno not in FSYNC_UNSUPPORTED_ERRORS:
                raise
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        method = (
            READ_CACHE_DROPPED if _resident_pages(fd, size) == 0
            else READ_CACHE_POSSIBLE
        )
        while True:
            checkpoint()
            block = os.read(fd, CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
        return digest.hexdigest(), method
    finally:
        os.close(fd)


def publish_without_overwrite(
    staged: Path,
    target: Path,
    staged_dir_fd: int,
    target_dir_fd: int,
    on_fallback=None,
) -> tuple[int, int]:
    """Publish relative to pinned directory FDs without replacing a target."""
    # 公開関数内でも、指定された親ディレクトリと保持FDの同一性を確認する。
    current_parent_stat = target.parent.stat()
    target_fd_stat = os.fstat(target_dir_fd)

    if not stat.S_ISDIR(current_parent_stat.st_mode):
        raise NotADirectoryError(
            errno.ENOTDIR,
            "コピー先の親パスがディレクトリではありません",
            str(target.parent),
        )

    if (
        current_parent_stat.st_dev != target_fd_stat.st_dev
        or current_parent_stat.st_ino != target_fd_stat.st_ino
    ):
        # 親ディレクトリ削除によってステージング領域も消失した場合は、
        # 置換エラーではなく、ステージング領域の消失として通知する。
        os.stat(
            staged.name,
            dir_fd=staged_dir_fd,
            follow_symlinks=False,
        )
        raise RuntimeError(
            "コピー先ディレクトリが公開処理中に置き換えられました"
        )

    staged_stat = os.stat(
        staged.name,
        dir_fd=staged_dir_fd,
        follow_symlinks=False,
    )

    staged_name = os.fsencode(staged.name)
    target_name = os.fsencode(target.name)
    identity = (staged_stat.st_dev, staged_stat.st_ino)

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)

    # 1. glibc の renameat2 関数（glibc 2.28 以降）
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int, ctypes.c_char_p,
            ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int

        ctypes.set_errno(0)
        result = renameat2(
            staged_dir_fd, staged_name,
            target_dir_fd, target_name,
            RENAME_NOREPLACE,
        )
        if result == 0:
            return identity
        error_number = ctypes.get_errno()
        if error_number == errno.EINVAL:
            # ファイルシステムが RENAME_NOREPLACE に対応していない。
            # システムコールでも結果は同じなので、代わりの方法へ進む。
            return _publish_fallback(
                staged, target, staged_dir_fd, target_dir_fd,
                staged_stat, on_fallback,
                "このファイルシステムは上書き防止付きの移動に対応していません",
            )
        if error_number != errno.ENOSYS:
            raise OSError(error_number, os.strerror(error_number), str(target))

    # 2. glibc に関数がない（古い glibc）か ENOSYS の場合は、システムコールを直接呼ぶ。
    #    番号は CPU の種類ごとに異なるため、確実に分かるものだけを使う。
    number = renameat2_syscall_number(
        os.uname().machine, ctypes.sizeof(ctypes.c_void_p) * 8
    )
    syscall = getattr(libc, "syscall", None)
    if number is not None and syscall is not None:
        syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        result = syscall(
            ctypes.c_long(number),
            ctypes.c_int(staged_dir_fd),
            ctypes.c_char_p(staged_name),
            ctypes.c_int(target_dir_fd),
            ctypes.c_char_p(target_name),
            ctypes.c_uint(RENAME_NOREPLACE),
        )
        if result == 0:
            return identity
        error_number = ctypes.get_errno()
        if error_number not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(error_number, os.strerror(error_number), str(target))
        reason = (
            "このファイルシステムは上書き防止付きの移動に対応していません"
            if error_number == errno.EINVAL
            else "カーネルが上書き防止付きの移動（renameat2）に対応していません"
        )
    else:
        reason = "上書き防止付きの移動（renameat2）を呼び出せない環境です"

    return _publish_fallback(
        staged, target, staged_dir_fd, target_dir_fd,
        staged_stat, on_fallback, reason,
    )


RENAME_NOREPLACE = 1

# renameat2 のシステムコール番号（Linux の各 CPU 向けの値）。
# 64ビットの CPU でも、32ビット版の Python からは 32ビットの番号を使う必要がある。
_RENAMEAT2_SYSCALL_NUMBERS = {
    ("x86_64", 64): 316,
    ("x86_64", 32): 353,     # 64ビットカーネル上の 32ビット x86
    ("i386", 32): 353,
    ("i686", 32): 353,
    ("aarch64", 64): 276,
    ("arm64", 64): 276,
    ("aarch64", 32): 382,    # 64ビットカーネル上の 32ビット ARM
    ("armv7l", 32): 382,
    ("armv6l", 32): 382,
    ("riscv64", 64): 276,
    ("loongarch64", 64): 276,
    ("ppc64le", 64): 357,
    ("ppc64", 64): 357,
    ("s390x", 64): 347,
}


def renameat2_syscall_number(machine: str, pointer_bits: int):
    """renameat2 のシステムコール番号を返す。分からない環境では None。

    誤った番号を呼ぶと別のシステムコールが実行されてしまうため、
    確実に分かっている組み合わせ以外は使わない。
    """
    return _RENAMEAT2_SYSCALL_NUMBERS.get((machine, pointer_bits))


_LINK_UNSUPPORTED_ERRORS = {
    errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV,
}


def _publish_fallback(staged, target, staged_dir_fd, target_dir_fd,
                      staged_stat, on_fallback, reason):
    """RENAME_NOREPLACE が使えない場合の、上書きしない公開方法。

    ファイル・シンボリックリンク: ハードリンクを作ってから一時側を消す
      （作成先に同名があれば link が EEXIST で失敗するため上書きしない）。
      ハードリンクを作れない場合は、名前の予約による方法を使う。
    フォルダー: 名前の予約による方法を使う。
    """
    identity = (staged_stat.st_dev, staged_stat.st_ino)

    if not stat.S_ISDIR(staged_stat.st_mode):
        try:
            os.link(
                staged.name, target.name,
                src_dir_fd=staged_dir_fd, dst_dir_fd=target_dir_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED_ERRORS:
                raise
        else:
            if on_fallback:
                on_fallback(f"{reason}。ハードリンク方式で公開しました: {target}")
            try:
                os.unlink(staged.name, dir_fd=staged_dir_fd)
            except OSError:
                pass  # 一時側に残った名前は、一時コピー領域の清掃で削除される
            return identity

    _publish_by_reservation(staged, target, staged_dir_fd, target_dir_fd,
                            stat.S_ISDIR(staged_stat.st_mode))
    if on_fallback:
        on_fallback(f"{reason}。名前を予約する方式で公開しました: {target}")
    return identity


def _publish_by_reservation(staged, target, staged_dir_fd, target_dir_fd,
                            is_dir: bool) -> None:
    """名前を予約してから置き換える、上書きしない公開方法。

    1. 公開先に空のフォルダー（または空のファイル）を排他的に作る。
       同名の項目が既にあれば、ここで失敗する（上書きしない）。
    2. 予約した項目がそのままであることを確かめてから、rename で置き換える。
       フォルダーの場合、予約に他の何かが入れられていれば rename 自体が
       失敗する（空でないフォルダーは置き換えられない）。
    """
    if is_dir:
        os.mkdir(target.name, 0o700, dir_fd=target_dir_fd)
    else:
        fd = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=target_dir_fd,
        )
        os.close(fd)
    reserved = os.stat(target.name, dir_fd=target_dir_fd, follow_symlinks=False)
    reserved_id = (reserved.st_dev, reserved.st_ino)

    try:
        current = os.stat(target.name, dir_fd=target_dir_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != reserved_id or (
            not is_dir and current.st_size != 0
        ):
            raise FileExistsError(
                errno.EEXIST, "公開先の予約が他の処理に置き換えられました", str(target)
            )
        os.rename(
            staged.name, target.name,
            src_dir_fd=staged_dir_fd, dst_dir_fd=target_dir_fd,
        )
    except BaseException:
        # 予約がまだ自分のものなら取り消す。
        try:
            current = os.stat(target.name, dir_fd=target_dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == reserved_id:
                if is_dir:
                    os.rmdir(target.name, dir_fd=target_dir_fd)
                else:
                    os.unlink(target.name, dir_fd=target_dir_fd)
        except OSError:
            pass
        raise


class CopyWorker(QThread):
    progress = Signal(object, object, float, str)
    log = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, source: Path, destination_parent: Path, verify: bool):
        super().__init__()
        self.source = source
        self.destination_parent = destination_parent
        self.verify = verify
        self._cancel = False
        self._paused = False
        self.total_bytes = 0
        self.done_bytes = 0
        self.started_at = 0.0
        self._verification_result = "未実施"
        self._history_written = False
        # ハードリンク: (dev, ino) → (ステージング内の相対位置, 表示用パス)
        self._hardlink_map = {}
        # コピー先フォルダーの (dev, ino) → コピー元フォルダーの属性の控え
        self._dir_metadata = {}
        self._root_dst_id = None
        self._staging_dir_fd = None
        self._tree_root_name = None
        self._tree_names = None
        # コピーしなかった特殊ファイルの (表示用パス, 種類) の一覧
        self.skipped_items: list[tuple[str, str]] = []
        # ディスクへの書き込み確定（fsync）の状況
        self.fsync_count = 0
        self.syncfs_count = 0
        self.durability_degraded = False
        # True: ファイル・フォルダーごとの fsync をやめ、公開前に syncfs で一括確定する
        self.batch_sync = False
        # 検証でコピー先を読み直した方法ごとの件数
        self.read_methods: dict[str, int] = {}
        self._copy_buffer = None
        self._use_cfr = True
        self._last_progress_at = 0.0
        self._cache_warning_shown = False
        self.current_stage = "コピー準備"
        self.current_item = str(source)

    def cancel(self):
        self._cancel = True
        self._paused = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def _checkpoint(self):
        while self._paused and not self._cancel:
            time.sleep(0.1)
        if self._cancel:
            raise InterruptedError("ユーザーによって中止されました。")

    PROGRESS_INTERVAL = 0.1  # 進捗表示の更新間隔（秒）

    def _emit_progress(self, current: Path):
        # 高速なSSDでは数千回/秒になるため、画面の更新は間隔を空ける。
        now = time.monotonic()
        if now - self._last_progress_at < self.PROGRESS_INTERVAL:
            return
        self._last_progress_at = now
        elapsed = max(now - self.started_at, 0.001)
        self.progress.emit(self.done_bytes, self.total_bytes,
                           self.done_bytes / elapsed, str(current))

    def _open_source_stream(self, src_path: Path):
        """Open the originally identified regular file without following symlinks."""
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        # 通常ファイルが FIFO に差し替えられても、開く時点で待ち続けないようにする。
        flags |= getattr(os, "O_NONBLOCK", 0)

        source_fd = os.open(src_path, flags)
        try:
            opened_stat = os.fstat(source_fd)

            if not stat.S_ISREG(opened_stat.st_mode):
                raise IOError(f"コピー元が通常ファイルではありません: {src_path}")
            os.set_blocking(source_fd, True)

            expected = getattr(self, "_expected_source_identity", None)
            actual = (opened_stat.st_dev, opened_stat.st_ino)

            if expected is not None and actual != expected:
                raise IOError(
                    f"コピー開始前にコピー元ファイルが置き換えられました: {src_path}"
                )

            stream = os.fdopen(source_fd, "rb", buffering=0)
            source_fd = -1
            return stream
        finally:
            if source_fd >= 0:
                os.close(source_fd)

    def _check_source_identity(self, src_path: Path):
        """Reject a source path that no longer identifies the original file."""
        try:
            current_stat = os.stat(src_path, follow_symlinks=False)
        except OSError as exc:
            raise IOError(
                f"コピー元ファイルを確認できません: {src_path}"
            ) from exc

        expected = getattr(self, "_expected_source_identity", None)
        actual = (current_stat.st_dev, current_stat.st_ino)

        if (
            not stat.S_ISREG(current_stat.st_mode)
            or expected is None
            or actual != expected
        ):
            raise IOError(
                f"処理中にコピー元ファイルが置き換えられました: {src_path}"
            )

    @staticmethod
    def _create_destination_stream(dst_path: Path):
        """非公開のステージング領域内に、新しいファイルを排他的に作成する。"""
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(dst_path, flags, 0o600)
        return os.fdopen(fd, "wb", buffering=0)

    def _copy_stream(self, src_path: Path, dst_path: Path):
        """ファイルの中身をコピーする。

        - 穴（スパース領域）は書かずに飛ばし、コピー先でも穴のまま残す
          （仮想マシンのディスクイメージなどが実サイズまで膨らまないように）
        - データ部分は copy_file_range を優先し、使えなければ read/write に切り替える
        """
        self.current_stage = "データ転送"
        self.current_item = str(src_path)
        with self._open_source_stream(src_path) as src, self._create_destination_stream(dst_path) as dst:
            src_fd, dst_fd = src.fileno(), dst.fileno()
            self._use_cfr = hasattr(os, "copy_file_range")
            position = 0
            for start, length in self._data_segments(src_fd):
                if start > position:
                    # 穴の部分は書かない（進捗だけ進める）
                    self.done_bytes += start - position
                    self._emit_progress(src_path)
                os.lseek(src_fd, start, os.SEEK_SET)
                os.lseek(dst_fd, start, os.SEEK_SET)
                position = start + self._copy_range(
                    src_path, src_fd, dst_fd, length
                )
            # 末尾が穴の場合や、穴だけのファイルは、長さだけ合わせる
            size = os.fstat(src_fd).st_size
            if size > position:
                os.ftruncate(dst_fd, size)
                self.done_bytes += size - position
                self._emit_progress(src_path)
            dst.flush()
            # ディスクへの書き込み確定（fsync）は、日時・権限の設定後に
            # _fsync_file でまとめて行う（属性も含めて確定させるため）。

    @staticmethod
    def _data_segments(fd: int):
        """ファイルのデータ部分を (開始位置, 長さ) で順に返す。長さ None は末尾まで。

        SEEK_DATA/SEEK_HOLE に対応していないファイルシステムでは、
        ファイル全体を1つのデータ部分として扱う。
        """
        seek_data = getattr(os, "SEEK_DATA", None)
        seek_hole = getattr(os, "SEEK_HOLE", None)
        if seek_data is None or seek_hole is None:
            yield 0, None
            return
        offset = 0
        while True:
            try:
                data = os.lseek(fd, offset, seek_data)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    return  # これ以降はデータなし（末尾まで穴）
                if offset == 0 and exc.errno in (errno.EINVAL, errno.EOPNOTSUPP):
                    yield 0, None
                    return
                raise
            hole = os.lseek(fd, data, seek_hole)
            if hole <= data:
                return
            yield data, hole - data
            offset = hole

    def _copy_range(self, src_path: Path, src_fd: int, dst_fd: int, length):
        """現在位置から length バイト（None なら末尾まで）コピーし、コピーした量を返す。"""
        copied_total = 0
        while length is None or copied_total < length:
            self._checkpoint()
            want = CHUNK_SIZE if length is None else min(CHUNK_SIZE, length - copied_total)
            if self._use_cfr:
                try:
                    copied = os.copy_file_range(src_fd, dst_fd, want)
                except OSError as exc:
                    fallback_errors = {
                        errno.EXDEV, errno.EINVAL, errno.ENOSYS,
                        errno.EOPNOTSUPP, errno.EPERM, errno.ENOTSUP,
                    }
                    if exc.errno not in fallback_errors:
                        raise
                    self.log.emit(
                        f"高速コピー非対応のため通常コピーに切替: {src_path.name} ({exc})"
                    )
                    self._use_cfr = False
                    continue
                if copied == 0:
                    break  # 末尾に達した（コピー中に短くなった場合を含む）
                copied_total += copied
                self.done_bytes += copied
                self._emit_progress(src_path)
                continue

            # 通常コピー：読み込み用のバッファを使い回す
            buffer = self._buffer()
            count = os.readv(src_fd, [memoryview(buffer)[:want]])
            if count == 0:
                break
            view = memoryview(buffer)[:count]
            while view:
                self._checkpoint()
                written = os.write(dst_fd, view)
                if written <= 0:
                    raise OSError("コピー先への書き込みが進みません。")
                self.done_bytes += written
                view = view[written:]
            copied_total += count
            self._emit_progress(src_path)
        return copied_total

    def _buffer(self) -> bytearray:
        if self._copy_buffer is None:
            self._copy_buffer = bytearray(CHUNK_SIZE)
        return self._copy_buffer

    def _copy_file(self, src: Path, dst: Path):
        self.current_stage = "ファイル処理"
        self.current_item = str(src)
        self._checkpoint()

        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
            self.log.emit(f"シンボリックリンクを複製（リンク先は追跡しません）: {src}")
            return

        # シンボリックリンクを辿らずに識別情報を取得する。
        source_stat = os.stat(src, follow_symlinks=False)
        if not stat.S_ISREG(source_stat.st_mode):
            raise IOError(f"コピー元が通常ファイルではありません: {src}")
        self._expected_source_identity = (
            source_stat.st_dev,
            source_stat.st_ino,
        )
        hardlink_key = (source_stat.st_dev, source_stat.st_ino)

        # 同じinodeをすでにコピーしている場合は、そのコピー先へリンクする。
        first_parent_fd = None
        if source_stat.st_nlink > 1 and hardlink_key in self._hardlink_map:
            parts, first_display = self._hardlink_map[hardlink_key]
            first_parent_fd = self._open_staged_parent(parts)
            first_destination = AnchoredPath.at(first_display, first_parent_fd)
        try:
            if first_parent_fd is not None:
                self.current_stage = "ハードリンク処理"
                self.current_item = str(src)
                self._checkpoint()

                self._check_source_identity(src)

                if self.verify:
                    self.current_stage = "SHA-256検証"

                    self._check_source_identity(src)
                    source_hash_before = sha256_file(
                        src, self._checkpoint
                    )
                    self._check_source_identity(src)

                    destination_hash = self._hash_destination(
                        first_destination
                    )

                    self._check_source_identity(src)
                    source_hash_after = sha256_file(
                        src, self._checkpoint
                    )
                    self._check_source_identity(src)

                    if not (
                        source_hash_before
                        == destination_hash
                        == source_hash_after
                    ):
                        raise IOError(
                            f"ハードリンク元のSHA-256検証不一致: {src}"
                        )

                    self.log.emit(
                        f"ハードリンク元のSHA-256検証 OK: {src.name}"
                    )

                self.current_stage = "ハードリンク作成"
                self.current_item = str(src)
                self._checkpoint()
                self._check_source_identity(src)
                try:
                    os.link(first_destination, dst)
                    self.done_bytes += source_stat.st_size
                    self._emit_progress(src)
                    self.log.emit(
                        f"ハードリンクを再現: {src} → {first_destination}"
                    )
                    return
                except OSError as exc:
                    fallback_errors = {
                        errno.EPERM,
                        errno.EOPNOTSUPP,
                        errno.ENOTSUP,
                        errno.EXDEV,
                        errno.EMLINK,
                    }
                    if exc.errno not in fallback_errors:
                        raise
                    self.log.emit(
                        f"ハードリンク非対応のため通常コピーに切替: {src} ({exc})"
                    )
        finally:
            if first_parent_fd is not None:
                os.close(first_parent_fd)

        # ステージング領域は自分専用の非公開フォルダーなので、ファイルごとの
        # 一時ファイルは作らず、最終的な名前で直接作成する。
        # これにより、検証したファイルそのものが公開され、日時・権限も保たれる。
        # 途中で失敗した場合は、ステージング領域ごと削除される。
        self._copy_stream(src, dst)

        self.current_stage = "属性コピー"
        self.current_item = str(src)
        self._check_source_identity(src)
        shutil.copystat(src, dst, follow_symlinks=False)
        self._check_source_identity(src)

        # 中身と日時・権限をまとめてディスクに確定させる。
        self.current_stage = "書き込み確定"
        self._fsync_file(dst)

        if self.verify:
            self.current_stage = "SHA-256検証"
            self.current_item = str(src)
            self._checkpoint()
            self._check_source_identity(src)
            source_hash_before = sha256_file(
                src, self._checkpoint
            )
            self._check_source_identity(src)

            # コピー先はキャッシュではなくストレージから読み直す。
            destination_hash = self._hash_destination(dst)

            self._check_source_identity(src)
            source_hash_after = sha256_file(
                src, self._checkpoint
            )
            self._check_source_identity(src)

            if not (
                source_hash_before
                == destination_hash
                == source_hash_after
            ):
                raise IOError(f"SHA-256検証不一致: {src}")

            self.log.emit(
                f"SHA-256検証 OK: {src.name} "
                f"({source_hash_before[:16]}…)"
            )

        self._check_source_identity(src)

        # 最初のコピーが正常に完了した後で対応関係を記録する。
        if source_stat.st_nlink > 1:
            self._hardlink_map[hardlink_key] = (self._staged_parts(dst.name), str(dst))

    def _copy_tree(self, src: AnchoredPath, dst: Path):
        """コピー元をフォルダーの FD を基準にたどってコピーする。

        - 再帰を使わない（Python の再帰の上限に関係なく、何階層でもたどれる）
        - 開いているフォルダーは、コピー元・コピー先とも常に現在の1つだけ
          （開けるファイル数の上限に関係しない）。親へは「..」で戻り、
          元の親と同じフォルダーであることを確かめる
        - コピー先もフォルダーの FD を基準に作る（パスの長さの上限 4096 バイトに
          関係しない）
        - src は親フォルダーの FD に固定された AnchoredPath。パスの途中の
          フォルダーが差し替えられても、コピー元のツリーの外は読まない
        """
        self.current_stage = "フォルダー処理"
        self.current_item = str(src)
        self._checkpoint()

        src_stat = os.stat(src, follow_symlinks=False)
        if stat.S_ISLNK(src_stat.st_mode):
            os.symlink(os.readlink(src), dst)
            return
        if stat.S_ISREG(src_stat.st_mode):
            self._copy_file(src, dst)
            return
        if not stat.S_ISDIR(src_stat.st_mode):
            raise IOError(f"未対応の種類のファイルです: {src}")

        # 表示用のパス。フォルダーに入るたびに名前を足し、出るたびに戻す
        # （毎回組み立て直すと、深い階層で時間が階層数の2乗に比例して増えるため）。
        src_display = str(src)
        dst_display = str(dst)
        display_lengths = []
        src_fd = dst_fd = None
        try:
            src_fd = self._open_source_dir(src, src_stat)
            # 作成時は所有者のみ読み書き可能にしておく。本来の権限は、
            # すべての中身をコピーし終えてから _apply_directory_metadata で設定する。
            # （途中で読み取り専用にすると、失敗時の清掃や公開ができなくなるため）
            os.mkdir(dst, 0o700)
            dst_fd = os.open(dst, _DIR_OPEN_FLAGS)
            self._tree_root_name = dst.name
            self._tree_names = names = []   # ルートからの相対位置（フォルダー名の並び）
            stack = [self._new_frame(src_fd, dst_fd)]

            while True:
                frame = stack[-1]
                if frame["index"] < len(frame["entries"]):
                    name, kind = frame["entries"][frame["index"]]
                    frame["index"] += 1
                    self._checkpoint()
                    child_src = AnchoredPath.at(f"{src_display}/{name}", src_fd)
                    child_dst = AnchoredPath.at(f"{dst_display}/{name}", dst_fd)

                    if kind in ("link", "file"):
                        self._copy_file(child_src, child_dst)
                        continue
                    if kind != "dir":
                        self.skipped_items.append((str(child_src), kind))
                        self.log.emit(f"未対応の特殊ファイルをスキップ（{kind}）: {child_src}")
                        continue

                    # 子フォルダーへ進む
                    self.current_stage = "フォルダー処理"
                    self.current_item = str(child_src)
                    child_stat = os.stat(child_src, follow_symlinks=False)
                    if not stat.S_ISDIR(child_stat.st_mode):
                        # 一覧を取得した後に種類が変わった
                        if stat.S_ISLNK(child_stat.st_mode) or stat.S_ISREG(child_stat.st_mode):
                            self._copy_file(child_src, child_dst)
                            continue
                        raise IOError(f"未対応の種類のファイルです: {child_src}")
                    child_src_fd = self._open_source_dir(child_src, child_stat)
                    try:
                        os.mkdir(name, 0o700, dir_fd=dst_fd)
                        child_dst_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dst_fd)
                    except BaseException:
                        os.close(child_src_fd)
                        raise
                    os.close(src_fd)
                    os.close(dst_fd)
                    src_fd, dst_fd = child_src_fd, child_dst_fd
                    names.append(name)
                    display_lengths.append((len(src_display), len(dst_display)))
                    src_display = f"{src_display}/{name}"
                    dst_display = f"{dst_display}/{name}"
                    stack.append(self._new_frame(src_fd, dst_fd))
                    continue

                # このフォルダーの中身をすべてコピーし終えた
                metadata = DirectoryMetadata(src_fd)
                self._dir_metadata[frame["dst_id"]] = metadata
                stack.pop()
                if not stack:
                    self._root_dst_id = frame["dst_id"]
                    break
                parent_src = self._open_parent(src_fd, stack[-1]["src_id"], "コピー元")
                try:
                    parent_dst = self._open_parent(dst_fd, stack[-1]["dst_id"], "コピー先")
                except BaseException:
                    os.close(parent_src)
                    raise
                os.close(src_fd)
                os.close(dst_fd)
                src_fd, dst_fd = parent_src, parent_dst
                names.pop()
                src_length, dst_length = display_lengths.pop()
                src_display = src_display[:src_length]
                dst_display = dst_display[:dst_length]
        finally:
            if src_fd is not None:
                os.close(src_fd)
            if dst_fd is not None:
                os.close(dst_fd)
            self._tree_names = None

    def _open_source_dir(self, src: AnchoredPath, expected_stat) -> int:
        """コピー元フォルダーを O_NOFOLLOW で開き、確認した実体と同じか確かめる。"""
        fd = os.open(src, _DIR_OPEN_FLAGS)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
            os.close(fd)
            raise IOError(f"処理中にコピー元フォルダーが置き換えられました: {src}")
        return fd

    @staticmethod
    def _open_parent(fd: int, expected_id, side: str) -> int:
        """「..」で親フォルダーへ戻り、元の親と同じフォルダーであることを確かめる。"""
        parent = os.open("..", _DIR_OPEN_FLAGS, dir_fd=fd)
        st = os.fstat(parent)
        if (st.st_dev, st.st_ino) != expected_id:
            os.close(parent)
            raise IOError(f"処理中に{side}のフォルダーが別の場所へ移動されました")
        return parent

    def _new_frame(self, src_fd: int, dst_fd: int) -> dict:
        src_st = os.fstat(src_fd)
        dst_st = os.fstat(dst_fd)
        return {
            "src_id": (src_st.st_dev, src_st.st_ino),
            "dst_id": (dst_st.st_dev, dst_st.st_ino),
            "entries": self._list_source(src_fd),
            "index": 0,
        }

    @staticmethod
    def _list_source(fd: int) -> list:
        """フォルダーの中身を (名前, 種類) の一覧にする。種類は開いている間に調べる。

        ファイルシステムに依存しない一定の順序（名前順）で処理する。
        ログの順番や、ハードリンクのどれを実体としてコピーするかが
        環境によって変わらないようにするため。
        """
        with os.scandir(fd) as it:
            entries = sorted(it, key=lambda entry: entry.name)
        result = []
        for entry in entries:
            if entry.is_symlink():
                kind = "link"
            elif entry.is_dir(follow_symlinks=False):
                kind = "dir"
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            else:
                try:
                    kind = special_file_kind(entry.stat(follow_symlinks=False).st_mode)
                except OSError:
                    kind = "不明な種類"
            result.append((entry.name, kind))
        return result

    def _staged_parts(self, name: str) -> tuple:
        """ステージング領域から見た相対位置（フォルダー名の並び）。"""
        if self._tree_names is None:
            return (name,)
        return (self._tree_root_name, *self._tree_names, name)

    def _open_staged_parent(self, parts: tuple) -> int:
        """ステージング領域内の項目の親フォルダーを開く（呼び出し側で閉じる）。"""
        fd = os.dup(self._staging_dir_fd)
        try:
            for component in parts[:-1]:
                child = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _fsync(self, fd: int, what: str) -> bool:
        """fd の内容をディスクに確定させる。

        ファイルシステムが fsync に対応していない場合（EINVAL など）は警告して
        False を返す。入出力エラーなど本当の失敗は例外として伝える。
        """
        try:
            os.fsync(fd)
            self.fsync_count += 1
            return True
        except OSError as exc:
            if exc.errno not in FSYNC_UNSUPPORTED_ERRORS:
                raise
            if not self.durability_degraded:
                self.log.emit(
                    "警告: このファイルシステムはディスクへの書き込み確定（fsync）に"
                    f"対応していません。停電時の安全性は保証できません: {what} ({exc})"
                )
            self.durability_degraded = True
            return False

    def _hash_destination(self, path: Path) -> str:
        """コピー先をストレージから読み直してハッシュを計算し、読み方を記録する。"""
        digest, method = sha256_file_from_storage(path, self._checkpoint)
        self.read_methods[method] = self.read_methods.get(method, 0) + 1
        if method == READ_CACHE_POSSIBLE and not self._cache_warning_shown:
            self._cache_warning_shown = True
            self.log.emit(
                "警告: このファイルシステムではキャッシュを通さない読み直しができず、"
                "検証がディスク上のデータではなくキャッシュを読んだ可能性があります: "
                f"{path}"
            )
        return digest

    def _verification_note(self) -> str:
        """完了メッセージ用：コピー先をどう読み直して照合したかの補足。"""
        if not self.verify or not self.read_methods:
            return ""
        if self.read_methods.get(READ_CACHE_POSSIBLE):
            return (
                f"\n  （{self.read_methods[READ_CACHE_POSSIBLE]:,} 件は、ディスクではなく"
                "キャッシュを読んだ可能性があります）"
            )
        return "\n  （コピー先はディスクから読み直して照合）"

    # None: 自動（Linux 5.8 以降なら一括確定）、True/False: 強制（試験用）
    BATCH_SYNC = None

    def _decide_sync_mode(self):
        if self.BATCH_SYNC is None:
            self.batch_sync = syncfs_available()
        else:
            self.batch_sync = bool(self.BATCH_SYNC)
        self.log.emit(
            "書き込み確定: "
            + ("公開前にファイルシステム単位で一括（syncfs）"
               if self.batch_sync else "ファイル・フォルダーごと（fsync）")
        )

    def _sync_staging(self, staging_fd: int) -> None:
        """一括モードで、公開前にステージング領域の内容をまとめて確定させる。

        中身・日時・権限・フォルダーの項目がすべてディスクに書かれる。
        入出力エラーは例外として伝わり、公開は行われない。
        """
        try:
            syncfs(staging_fd)
            self.syncfs_count += 1
        except OSError as exc:
            if exc.errno not in FSYNC_UNSUPPORTED_ERRORS | {errno.ENOSYS}:
                raise
            self.log.emit(
                "警告: このファイルシステムでは一括の書き込み確定（syncfs）が"
                f"できません。停電時の安全性は保証できません ({exc})"
            )
            self.durability_degraded = True

    def _fsync_file(self, path: Path) -> bool:
        if self.batch_sync:
            return True  # 公開前の syncfs でまとめて確定する
        # 所有者に読み取り権限がない場合も開けるようにする（例: 0204）。
        fd = open_own_file_readonly(path)
        try:
            return self._fsync(fd, str(path))
        finally:
            os.close(fd)

    def _apply_directory_metadata(self, root_dst: Path):
        """コピー元フォルダーの日時・権限などを、深い階層から順に設定する。

        公開対象のルートフォルダーは除く（公開後に FD 経由で設定する）。
        読み取り専用のルートフォルダーは、別のフォルダーへ移動（公開）
        できないためである。再帰を使わずにたどる。
        """
        if self._root_dst_id is None:
            return  # ルートがフォルダーではない
        self.current_stage = "フォルダー属性コピー"

        def visit(fd):
            self._checkpoint()
            with os.scandir(fd) as it:
                return [e.name for e in it if e.is_dir(follow_symlinks=False)]

        def leave(fd, parent_fd, name):
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
            if identity == self._root_dst_id:
                return
            metadata = self._dir_metadata.get(identity)
            if metadata is None:
                return
            # 親へ戻る「..」は、権限を変える前に walk_directories が開いている。
            metadata.apply_to_fd(fd)
            # フォルダーの項目（ファイル名・リンク）と属性を確定させる。
            if not self.batch_sync:
                self._fsync(fd, name)

        walk_directories(self._staging_dir_fd, root_dst.name, visit, leave)

    def _root_directory_metadata(self, root_dst: Path):
        return self._dir_metadata.get(self._root_dst_id)

    def _skipped_note(self) -> str:
        """中止・エラー時の履歴に添える、それまでにスキップした項目の一覧。"""
        if not self.skipped_items:
            return ""
        return (
            f"\n\n停止までにスキップした特殊ファイル（{len(self.skipped_items):,} 件）:\n"
            f"{format_skipped(self.skipped_items, SKIP_HISTORY_LIMIT)}"
        )

    def _write_history(self, result: str, target: Path, details: str):
        if self._history_written:
            return
        elapsed = max(time.monotonic() - self.started_at, 0.0)
        record_history(self.source, target, result, self.done_bytes,
                       elapsed, self._verification_result, details)
        self._history_written = True

    def run(self):
        target = self.destination_parent / self.source.name
        staging_parent = None
        staging_fd = None
        staging_dir_fd = None
        root_dir_fd = None
        source_parent_fd = None
        try:
            self.current_stage = "コピー前の確認"
            self.current_item = str(self.source)
            if not self.source.exists() and not self.source.is_symlink():
                raise FileNotFoundError(f"コピー元が見つかりません: {self.source}")
            if not self.destination_parent.is_dir():
                raise NotADirectoryError(f"コピー先フォルダーがありません: {self.destination_parent}")
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"コピー先に同名の項目があります。上書きしません: {target}")

            _, self.total_bytes, required_space = scan_source_detail(self.source)
            # 何時間もコピーした後で容量不足になるのを避けるため、先に確認する。
            check_free_space(self.destination_parent, required_space)
            self.started_at = time.monotonic()

            # コピー先親ディレクトリのFDを取得する。
            staging_fd = os.open(
                self.destination_parent,
                os.O_RDONLY | os.O_DIRECTORY
            )

            # /proc/self/fd 経由で、取得済みFDが指すディレクトリに
            # ステージング領域を作成する。
            # 親ディレクトリのパスが置換されても作成先は変わらない。
            staging_fd_path = Path(f"/proc/self/fd/{staging_fd}")
            staging_parent = Path(tempfile.mkdtemp(
                prefix=STAGING_PREFIX,
                dir=str(staging_fd_path)
            ))

            # ステージングディレクトリ自身もFDで固定する。
            staging_dir_fd = os.open(
                staging_parent,
                os.O_RDONLY | os.O_DIRECTORY
            )

            # 使用中であることを示すロック。異常終了時はOSが自動で解放するため、
            # ロックされていない一時領域は「残骸」と判断できる。
            fcntl.flock(staging_dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._staging_dir_fd = staging_dir_fd

            # 現在のコピー先パスと、保持しているFDの参照先を比較する。
            # 一致しなければ、処理中に親ディレクトリが置換されている。
            destination_stat = self.destination_parent.stat()
            staging_fd_stat = os.fstat(staging_fd)

            if (
                destination_stat.st_dev != staging_fd_stat.st_dev
                or destination_stat.st_ino != staging_fd_stat.st_ino
            ):
                raise RuntimeError(
                    "コピー先ディレクトリが処理中に置き換えられました"
                )
            staged_target = staging_parent / self.source.name

            self.log.emit(f"コピー開始: {self.source} → {target}")
            self.log.emit("方式: copy_file_range優先、利用不可時は通常コピーへ切替")
            self.log.emit(f"SHA-256検証: {'有効' if self.verify else '無効'}")
            self.log.emit("安全対策: 一時領域でコピーし、完了後に上書きせず公開")
            # コピー元の親フォルダーを FD で固定し、以降はその FD を基準にたどる。
            # （利用者が指定したパスの解決はここで一度だけ行う）
            if not self.source.name or self.source.name in (".", ".."):
                raise ValueError(f"コピー元のパスが不正です: {self.source}")
            source_parent_fd = os.open(
                self.source.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            anchored_source = AnchoredPath.at(self.source, source_parent_fd)

            self._decide_sync_mode()
            self._copy_tree(anchored_source, staged_target)
            self._apply_directory_metadata(staged_target)

            # ルートがフォルダーの場合、公開後に属性を設定するため FD を保持する。
            staged_stat = os.stat(
                staged_target.name,
                dir_fd=staging_dir_fd,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(staged_stat.st_mode):
                root_dir_fd = os.open(
                    staged_target.name, _DIR_OPEN_FLAGS, dir_fd=staging_dir_fd
                )
                # 公開する前に、ルートフォルダーの項目をディスクに確定させる。
                # （中のファイル・サブフォルダーは確定済み）
                if not self.batch_sync:
                    self.current_stage = "書き込み確定"
                    self._fsync(root_dir_fd, str(staged_target))

            if self.batch_sync:
                self.current_stage = "書き込み確定（一括）"
                self.current_item = str(staged_target)
                self._sync_staging(staging_fd)

            self.current_stage = "最終確認・公開"
            self.current_item = str(target)
            self._checkpoint()

            # 公開直前にもコピー先パスと保持FDの同一性を確認する。
            destination_stat = self.destination_parent.stat()
            staging_fd_stat = os.fstat(staging_fd)

            if (
                destination_stat.st_dev != staging_fd_stat.st_dev
                or destination_stat.st_ino != staging_fd_stat.st_ino
            ):
                raise RuntimeError(
                    "コピー先ディレクトリが公開直前に置き換えられました"
                )

            published_identity = publish_without_overwrite(
                staged_target,
                target,
                staging_dir_fd,
                staging_fd,
                self.log.emit,  # 代わりの公開方法を使ったときの記録用
            )

            # 公開直後に、コピー先パスと保持FDの親ディレクトリが
            # 同一のディレクトリ実体を指していることを確認する。
            published_parent_stat = self.destination_parent.stat()
            published_parent_fd_stat = os.fstat(staging_fd)

            if (
                published_parent_stat.st_dev != published_parent_fd_stat.st_dev
                or published_parent_stat.st_ino != published_parent_fd_stat.st_ino
            ):
                raise RuntimeError(
                    "コピー先ディレクトリが公開処理中に置き換えられました。"
                    "公開済みデータは移動先のディレクトリに残っている可能性があります。"
                )

            # 公開先の実体が、公開前に確認したステージング対象と
            # 同一の inode であることを確認する。
            published_stat = os.stat(
                target.name,
                dir_fd=staging_fd,
                follow_symlinks=False,
            )

            if (
                published_stat.st_dev != published_identity[0]
                or published_stat.st_ino != published_identity[1]
            ):
                raise RuntimeError(
                    "公開先のファイルまたはフォルダーが公開直後の確認で置き換わりました"
                )

            if root_dir_fd is not None:
                # 公開までは所有者専用（0700）のまま。公開後に本来の属性を設定する。
                self.current_stage = "フォルダー属性コピー（公開後）"
                try:
                    root_metadata = self._root_directory_metadata(staged_target)
                    if root_metadata is None:
                        raise OSError(errno.ENOENT, "フォルダー属性の控えがありません")
                    root_metadata.apply_to_fd(root_dir_fd)
                    self._fsync(root_dir_fd, str(target))
                except OSError as exc:
                    raise RuntimeError(
                        "コピーは公開済みですが、フォルダーの日時・権限の設定に"
                        f"失敗しました: {target} ({exc})"
                    ) from exc

            # 公開（rename）の結果をディスクに確定させる。これをしないと、
            # 直後の停電で、公開したはずのコピーが消えることがある。
            self.current_stage = "書き込み確定（公開後）"
            try:
                self._fsync(staging_fd, str(self.destination_parent))
                self._fsync(staging_dir_fd, str(staging_parent))
            except OSError as exc:
                raise RuntimeError(
                    "コピーは公開済みですが、ディスクへの書き込み確定に失敗しました。"
                    f"停電などで失われる可能性があります: {target} ({exc})"
                ) from exc

            if not self.verify:
                self._verification_result = "検証なし"
            elif self.read_methods.get(READ_CACHE_POSSIBLE):
                self._verification_result = VERIFIED_MAYBE_CACHE
            else:
                self._verification_result = VERIFIED_FROM_STORAGE

            elapsed = max(time.monotonic() - self.started_at, 0.001)
            self.progress.emit(self.done_bytes, self.total_bytes,
                               self.done_bytes / elapsed, "完了")
            durability = (
                "一部未確定（このファイルシステムは fsync に未対応）"
                if self.durability_degraded else "確定済み"
            )
            summary = (
                f"転送量: {self.done_bytes:,} bytes\n"
                f"所要時間: {elapsed:.2f} 秒\n検証: {self._verification_result}"
                f"{self._verification_note()}\n"
                f"ディスクへの書き込み: {durability}"
            )
            if self.skipped_items:
                count = len(self.skipped_items)
                self.log.emit(f"注意: コピーしなかった特殊ファイルが {count:,} 件あります。")
                self._write_history(
                    RESULT_SUCCESS_WITH_SKIPS, target,
                    f"コピー完了（特殊ファイル {count:,} 件はコピーしていません）\n"
                    f"スキップした項目:\n"
                    f"{format_skipped(self.skipped_items, SKIP_HISTORY_LIMIT)}",
                )
                self.completed.emit(
                    f"コピーが完了しましたが、コピーしなかった項目が "
                    f"{count:,} 件あります。\n{target}\n\n"
                    f"{summary}\n\n"
                    f"■ スキップした特殊ファイル（コピーできない種類のため）\n"
                    f"{format_skipped(self.skipped_items, SKIP_MESSAGE_LIMIT)}\n\n"
                    f"一覧はコピー履歴の詳細でも確認できます。"
                )
            else:
                self._write_history(RESULT_SUCCESS, target, "コピー完了")
                self.completed.emit(
                    f"コピーが完了しました。\n{target}\n\n{summary}"
                )
        except InterruptedError as exc:
            detail = (f"{exc}\n処理段階: {self.current_stage}"
                      f"\n対象: {self.current_item}")
            self.log.emit(detail)
            self._write_history("中止", target, detail + self._skipped_note())
            self.failed.emit(
                detail + "\n\n一時コピー領域の清掃を行います。"
            )
        except Exception as exc:
            detail = (f"{type(exc).__name__}: {exc}"
                      f"\n処理段階: {self.current_stage}"
                      f"\n対象: {self.current_item}")
            self.log.emit("エラー詳細: " + detail)
            self._write_history("エラー", target, detail + self._skipped_note())
            self.failed.emit(
                detail + "\n\n一時コピー領域の清掃を行います。"
            )
        finally:
            try:
                if root_dir_fd is not None:
                    os.close(root_dir_fd)
                if source_parent_fd is not None:
                    os.close(source_parent_fd)
                if staging_parent is not None and staging_fd is not None:
                    try:
                        os.stat(
                            staging_parent.name,
                            dir_fd=staging_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        self.log.emit(
                            f"一時コピー領域は既に存在しないため清掃不要: "
                            f"{staging_parent}"
                        )
                    else:
                        try:
                            # 読み取り専用フォルダーも削除できる自前の削除処理。
                            # （Python 3.10 でも動作する）
                            remove_tree_at(staging_fd, staging_parent.name)
                            self.log.emit(
                                f"一時コピー領域の清掃に成功: {staging_parent}"
                            )
                        except OSError as cleanup_exc:
                            self.log.emit(
                                f"警告：一時コピー領域の清掃に失敗: "
                                f"{staging_parent} ({cleanup_exc})"
                            )
            finally:
                try:
                    if staging_dir_fd is not None:
                        os.close(staging_dir_fd)  # ロックも解放される
                finally:
                    if staging_fd is not None:
                        os.close(staging_fd)



def load_history(limit: int = 500) -> list[tuple]:
    """Load recent copy history without modifying the database."""
    if not DB_PATH.exists():
        return []

    with sqlite3.connect(DB_PATH) as db:
        return db.execute(
            """SELECT id, started_at, source, destination, result,
                      bytes_copied, elapsed_seconds, verification, details
               FROM copy_history
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


class HistoryDialog(QDialog):
    """Display saved copy history and the details of the selected record."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("UltraCopy - コピー履歴")
        self.resize(1150, 680)
        self.rows = []
        self.filtered_rows = []

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("コピー履歴"))
        top.addStretch()

        refresh_button = QPushButton("再読み込み")
        refresh_button.clicked.connect(self.load_records)
        top.addWidget(refresh_button)

        csv_button = QPushButton("CSV出力")
        csv_button.clicked.connect(self.export_csv)
        top.addWidget(csv_button)

        layout.addLayout(top)

        # 履歴検索・絞り込み
        filter_layout = QHBoxLayout()

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "コピー元・コピー先を検索..."
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.search_edit, 3)

        self.result_filter = QComboBox()
        self.result_filter.addItems([
            "すべて", RESULT_SUCCESS, RESULT_SUCCESS_WITH_SKIPS, "エラー", "中止"
        ])
        self.result_filter.currentTextChanged.connect(
            self.apply_filters
        )
        filter_layout.addWidget(self.result_filter, 1)

        self.verification_filter = QComboBox()
        self.verification_filter.addItems([
            "すべて",
            VERIFIED_FROM_STORAGE,
            VERIFIED_MAYBE_CACHE,
            "検証なし",
            "未実施",
        ])
        self.verification_filter.currentTextChanged.connect(
            self.apply_filters
        )
        filter_layout.addWidget(self.verification_filter, 1)

        layout.addLayout(filter_layout)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "ID", "日時", "結果", "コピー元",
            "コピー先", "転送量", "所要時間", "検証"
        ])
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setAlternatingRowColors(True)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self.table.currentCellChanged.connect(
            self.show_selected_details
        )
        layout.addWidget(self.table, 3)

        layout.addWidget(QLabel("選択した履歴の詳細"))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(170)
        layout.addWidget(self.detail, 1)

        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

        self.load_records()

    @staticmethod
    def format_bytes(value):
        value = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:,.2f} {unit}"
            value /= 1024

    def load_records(self):
        """Reload all history records from SQLite."""
        try:
            self.rows = load_history()
        except Exception as exc:
            QMessageBox.warning(
                self, "履歴読み込みエラー", str(exc)
            )
            return

        self.apply_filters()

    def apply_filters(self, *_):
        """Filter history by path text, result, and verification."""
        keyword = self.search_edit.text().strip().casefold()
        result_filter = self.result_filter.currentText()
        verification_filter = self.verification_filter.currentText()

        filtered = []

        for record in self.rows:
            source = str(record[2])
            destination = str(record[3])
            result = str(record[4])
            verification = str(record[7])

            if keyword:
                searchable = f"{source} {destination}".casefold()
                if keyword not in searchable:
                    continue

            if result_filter != "すべて" and result != result_filter:
                continue

            if (
                verification_filter != "すべて"
                and verification != verification_filter
            ):
                continue

            filtered.append(record)

        self.filtered_rows = filtered
        self.table.setRowCount(0)

        for record in self.filtered_rows:
            row_number = self.table.rowCount()
            self.table.insertRow(row_number)

            values = [
                str(record[0]),
                str(record[1]),
                str(record[4]),
                str(record[2]),
                str(record[3]),
                self.format_bytes(record[5]),
                f"{record[6]:.3f} 秒",
                str(record[7]),
            ]

            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                # 「成功（スキップあり）」は結果欄を注意色で表示する。
                if column == 2 and record[4] == RESULT_SUCCESS_WITH_SKIPS:
                    item.setForeground(QColor("#b36b00"))
                self.table.setItem(row_number, column, item)

        if self.filtered_rows:
            self.table.selectRow(0)
            self.show_selected_details(0, 0, -1, -1)
        else:
            self.detail.setPlainText(
                "条件に一致するコピー履歴はありません。"
            )

    def export_csv(self):
        """Export currently filtered history records to a CSV file."""
        if not self.filtered_rows:
            QMessageBox.information(
                self,
                "CSV出力",
                "出力できる履歴がありません。"
            )
            return

        filename, _ = QFileDialog.getSaveFileName(
            self,
            "コピー履歴をCSV形式で保存",
            str(Path.home() / "コピー履歴.csv"),
            "CSVファイル (*.csv)"
        )

        if not filename:
            return

        if not filename.lower().endswith(".csv"):
            filename += ".csv"

        headers = [
            "ID",
            "日時",
            "結果",
            "コピー元",
            "コピー先",
            "転送量(bytes)",
            "所要時間(秒)",
            "検証",
            "詳細",
        ]

        try:
            with open(
                filename,
                "w",
                encoding="utf-8-sig",
                newline=""
            ) as file:
                writer = csv.writer(file)
                writer.writerow(headers)

                for record in self.filtered_rows:
                    writer.writerow([
                        record[0],
                        record[1],
                        record[4],
                        record[2],
                        record[3],
                        record[5],
                        record[6],
                        record[7],
                        record[8],
                    ])

            QMessageBox.information(
                self,
                "CSV出力完了",
                f"{len(self.filtered_rows)}件の履歴を保存しました。\\n"
                f"保存先:\\n{filename}"
            )

        except Exception as exc:
            QMessageBox.critical(
                self,
                "CSV出力エラー",
                f"CSVファイルを保存できませんでした。\\n{exc}"
            )

    def show_selected_details(
        self, current_row, current_column, previous_row, previous_column
    ):
        if current_row < 0 or current_row >= len(self.filtered_rows):
            return

        record = self.filtered_rows[current_row]
        self.detail.setPlainText(
            f"履歴ID: {record[0]}\n"
            f"日時: {record[1]}\n"
            f"結果: {record[4]}\n"
            f"コピー元: {record[2]}\n"
            f"コピー先: {record[3]}\n"
            f"転送量: {self.format_bytes(record[5])}\n"
            f"所要時間: {record[6]:.3f} 秒\n"
            f"検証: {record[7]}\n"
            f"詳細:\n{record[8]}"
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"UltraCopy for Ubuntu {APP_VERSION}")
        self.resize(940, 690)
        self.worker = None
        self._closing = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        title = QLabel("UltraCopy for Ubuntu")
        title.setStyleSheet("font-size: 23px; font-weight: bold;")
        subtitle = QLabel(f"Ver.{APP_VERSION} — 高速コピーエンジン・性能最適化")
        root.addWidget(title)
        root.addWidget(subtitle)

        paths = QGroupBox("コピー設定")
        grid = QGridLayout(paths)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("コピーするファイルまたはフォルダー")
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("コピー先の親フォルダー")
        source_btn = QPushButton("コピー元を選択…")
        dest_btn = QPushButton("コピー先を選択…")
        source_btn.clicked.connect(self.choose_source)
        dest_btn.clicked.connect(self.choose_dest)
        grid.addWidget(QLabel("コピー元"), 0, 0)
        grid.addWidget(self.source_edit, 0, 1)
        grid.addWidget(source_btn, 0, 2)
        grid.addWidget(QLabel("コピー先"), 1, 0)
        grid.addWidget(self.dest_edit, 1, 1)
        grid.addWidget(dest_btn, 1, 2)
        self.verify_check = QCheckBox("コピー後にSHA-256でデータを検証する（推奨）")
        self.verify_check.setChecked(True)
        grid.addWidget(self.verify_check, 2, 1, 1, 2)
        root.addWidget(paths)

        actions = QHBoxLayout()
        self.start_btn = QPushButton("コピー開始")
        self.pause_btn = QPushButton("一時停止")
        self.resume_btn = QPushButton("再開")
        self.cancel_btn = QPushButton("中止")
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_copy)
        self.pause_btn.clicked.connect(self.pause_copy)
        self.resume_btn.clicked.connect(self.resume_copy)
        self.cancel_btn.clicked.connect(self.cancel_copy)
        for button in (self.start_btn, self.pause_btn, self.resume_btn, self.cancel_btn):
            button.setMinimumHeight(38)
            actions.addWidget(button)
        root.addLayout(actions)

        history_row = QHBoxLayout()
        self.history_btn = QPushButton("コピー履歴を表示")
        self.history_btn.setMinimumHeight(34)
        self.history_btn.clicked.connect(self.show_history)
        history_row.addWidget(self.history_btn)
        history_row.addStretch()
        root.addLayout(history_row)

        status = QGroupBox("進捗")
        sg = QGridLayout(status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.status_label = QLabel("待機中")
        self.speed_label = QLabel("速度: —")
        self.bytes_label = QLabel("転送量: —")
        self.eta_label = QLabel("残り時間: —")
        sg.addWidget(self.progress, 0, 0, 1, 3)
        sg.addWidget(self.status_label, 1, 0, 1, 3)
        sg.addWidget(self.speed_label, 2, 0)
        sg.addWidget(self.bytes_label, 2, 1)
        sg.addWidget(self.eta_label, 2, 2)
        root.addWidget(status)

        root.addWidget(QLabel("処理ログ"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        root.addWidget(self.log_view, 1)
        note = QLabel(
            "安全機能: 同名項目は上書きしません。通常ファイルは一時ファイルへコピーしてから公開します。"
            "中止・エラー時は一時コピー領域を自動清掃します。バックアップ用途では十分に検証してから使用してください。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a5a00;")
        root.addWidget(note)

    def show_history(self):
        dialog = HistoryDialog(self)
        dialog.exec()

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "コピー元ファイルを選択")
        if not path:
            path = QFileDialog.getExistingDirectory(self, "コピー元フォルダーを選択")
        if path:
            self.source_edit.setText(path)

    def choose_dest(self):
        path = QFileDialog.getExistingDirectory(self, "コピー先フォルダーを選択")
        if path:
            self.dest_edit.setText(path)

    def append_log(self, message):
        self.log_view.append(message)

    def start_copy(self):
        src_text, dst_text = self.source_edit.text().strip(), self.dest_edit.text().strip()
        if not src_text or not dst_text:
            QMessageBox.warning(self, "入力不足", "コピー元とコピー先を指定してください。")
            return
        source, destination = Path(src_text).expanduser(), Path(dst_text).expanduser()
        if not source.exists() and not source.is_symlink():
            QMessageBox.warning(self, "コピー元エラー", "コピー元が存在しません。")
            return
        if not destination.is_dir():
            QMessageBox.warning(self, "コピー先エラー", "コピー先には既存のフォルダーを指定してください。")
            return
        try:
            if source.resolve() == destination.resolve():
                QMessageBox.warning(self, "コピー先エラー", "コピー元とコピー先が同じです。")
                return
            if source.is_dir() and destination.resolve().is_relative_to(source.resolve()):
                QMessageBox.warning(self, "コピー先エラー", "コピー元フォルダーの内部にはコピーできません。")
                return
        except OSError as exc:
            QMessageBox.warning(self, "パス確認エラー", str(exc))
            return
        target = destination / source.name
        if target.exists() or target.is_symlink():
            QMessageBox.warning(self, "上書き防止", "コピー先に同名の項目があります。上書きしないため、別のコピー先を選んでください。")
            return

        self.offer_stale_staging_cleanup(destination)

        verify_enabled = self.verify_check.isChecked()
        self.append_log(
            f"SHA-256設定確認: チェックボックス={'ON' if verify_enabled else 'OFF'}"
        )
        self.worker = CopyWorker(source, destination, verify_enabled)
        self.worker.finished.connect(self.worker_finished)
        self.worker.progress.connect(self.update_progress)
        self.worker.log.connect(self.append_log)
        self.worker.completed.connect(self.copy_completed)
        self.worker.failed.connect(self.copy_failed)
        self.set_running(True)
        self.progress.setValue(0)
        self.status_label.setText("コピー準備中…")
        self.worker.start()

    def update_progress(self, done, total, speed, current):
        percent = done * 100 / total if total > 0 else 100.0
        self.progress.setValue(min(1000, int(percent * 10)))
        self.status_label.setText(f"{percent:.1f}%  |  {current}")
        self.speed_label.setText(f"速度: {self.format_size(speed)}/秒")
        self.bytes_label.setText(f"転送量: {self.format_size(done)} / {self.format_size(total)}")
        remaining = max(total - done, 0)
        self.eta_label.setText(f"残り時間: {self.format_time(remaining / speed) if speed > 0 else '計算中'}")

    @staticmethod
    def format_size(value):
        value = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    @staticmethod
    def format_time(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def pause_copy(self):
        if self.worker and self.worker.isRunning():
            self.worker.pause()
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(True)
            self.status_label.setText("一時停止中")

    def resume_copy(self):
        if self.worker and self.worker.isRunning():
            self.worker.resume()
            self.pause_btn.setEnabled(True)
            self.resume_btn.setEnabled(False)

    def cancel_copy(self):
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(self, "コピー中止", "コピーを中止しますか？\n作成済みのファイルは残る場合があります。")
            if answer == QMessageBox.StandardButton.Yes:
                self.worker.cancel()
                self.status_label.setText("中止処理中…")

    def set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(running)

    def copy_completed(self, message):
        self.set_running(False)
        self.progress.setValue(1000)
        skipped = len(self.worker.skipped_items) if self.worker else 0
        if skipped:
            self.status_label.setText(
                f"完了（コピーしなかった特殊ファイル {skipped:,} 件）"
            )
        else:
            self.status_label.setText("完了")
        self.append_log(message.replace("\n", " | "))
        if self._closing:
            return
        if skipped:
            # 見落とさないよう、通常の完了とは別のアイコン（注意）で表示する。
            QMessageBox.warning(self, "コピー完了（一部スキップ）", message)
        else:
            QMessageBox.information(self, "コピー完了", message)

    def copy_failed(self, message):
        self.set_running(False)
        self.status_label.setText("停止／エラー")
        self.append_log(f"エラー: {message}")
        if not self._closing:
            QMessageBox.warning(self, "コピー停止", message)

    def offer_stale_staging_cleanup(self, destination: Path):
        """前回の異常終了で残った一時コピー領域があれば、削除を提案する。"""
        try:
            stale = find_stale_staging(destination)
        except OSError:
            return
        if not stale:
            return

        total = sum(size for _, size in stale)
        names = "\n".join(f"・{name}" for name, _ in stale)
        answer = QMessageBox.question(
            self,
            "一時コピー領域の残り",
            "コピー先フォルダーに、以前の強制終了などで残った一時コピー領域が"
            f"{len(stale)}個あります（合計 {self.format_size(total)}）。\n\n"
            f"{names}\n\n"
            "削除しますか？（いま行うコピーには影響しません）",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.append_log("残っていた一時コピー領域は削除せずに残しました。")
            return

        errors = remove_stale_staging(destination, [name for name, _ in stale])
        if errors:
            QMessageBox.warning(
                self, "一時コピー領域の削除",
                "一部を削除できませんでした。\n" + "\n".join(errors),
            )
        else:
            self.append_log(
                f"残っていた一時コピー領域を{len(stale)}個削除しました。"
            )

    def worker_finished(self):
        # コピー中に「閉じる」が選ばれていた場合は、清掃完了後に閉じる。
        if self._closing:
            # finished はスレッド終了の直前に送られるため、完全終了を待つ。
            if self.worker is not None:
                self.worker.wait()
            self.close()

    def closeEvent(self, event):
        """コピー中にウィンドウを閉じた場合は、中止と清掃を待ってから終了する。"""
        if self.worker is None or not self.worker.isRunning():
            event.accept()
            return

        if not self._closing:
            answer = QMessageBox.question(
                self,
                "コピー中",
                "コピー中です。中止して終了しますか？\n"
                "一時コピー領域を清掃してから終了します。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
            self.worker.cancel()
            self.status_label.setText("中止して終了します…（一時コピー領域を清掃中）")
            self.setEnabled(False)

        # 実際の終了は worker_finished から行う。
        event.ignore()


def main():
    if sys.version_info < (3, 10):
        sys.exit(
            "UltraCopy には Python 3.10 以上が必要です"
            f"（現在: {sys.version.split()[0]}）"
        )
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
