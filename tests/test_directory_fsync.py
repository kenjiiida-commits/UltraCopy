"""レビュー指摘 9：フォルダーの fsync（停電時に公開済みのコピーが消えることへの対策）。

停電そのものは再現できないため、os.fsync と syncfs の呼び出しを記録して
「何を・どの順で」ディスクに確定させているかを確認する。

確定の方法は2つあり、両方を試験する（Ver.0.7.7 以降）。
  個別モード: ファイル・フォルダーごとに fsync（Linux 5.8 より前）
  一括モード: 公開前に syncfs でファイルシステム単位にまとめて確定（Linux 5.8 以降）

A. フォルダーのコピー
   - すべてのファイルを、日時・権限の設定後に fsync している
   - すべてのフォルダーを、深い階層から順に、公開前に fsync している
   - 公開後に、公開先の親フォルダーを fsync している
B. ファイル1つのコピーでも、ファイル → 公開 → 親フォルダーの順に確定している
C. fsync 非対応（EINVAL）のファイルシステムでは、警告してコピーは完了する
D. 公開前のフォルダーの fsync が入出力エラーなら、公開せずに停止する
E. 公開後の親フォルダーの fsync が入出力エラーなら、「公開済み」と分かる形で報告する
"""
from pathlib import Path
import errno
import os
import shutil
import stat
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main  # noqa: E402

RESULTS = []
MTIME_NS = 1_600_000_000_123_456_789


def check(condition, description):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    RESULTS.append(bool(condition))


def snapshot_staging(parent_fd):
    """syncfs が呼ばれた時点のステージング領域の状態（相対パス → 権限・更新日時）。"""
    result = {}
    base = os.readlink(f"/proc/self/fd/{parent_fd}")
    for root, dirs, files in os.walk(base):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.stat(path, follow_symlinks=False)
            parts = Path(path).relative_to(base).parts
            if parts and parts[0].startswith(main.STAGING_PREFIX):
                key = "/".join(parts[1:])
                result[key] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns)
    return result


class Recorder:
    """os.fsync と公開処理の呼び出しを順番に記録する。"""

    def __init__(self, fail=None):
        self.events = []
        self.fail = fail  # (判定関数, errno) または None
        self._fsync = main.os.fsync
        self._publish = main.publish_without_overwrite
        self._syncfs = main.syncfs

    def __enter__(self):
        def fsync(fd):
            st = os.fstat(fd)
            event = {
                "kind": "fsync",
                "dir": stat.S_ISDIR(st.st_mode),
                "id": (st.st_dev, st.st_ino),
                "mode": stat.S_IMODE(st.st_mode),
                "mtime_ns": st.st_mtime_ns,
                "path": os.readlink(f"/proc/self/fd/{fd}"),
            }
            self.events.append(event)
            if self.fail and self.fail[0](event, self.events):
                raise OSError(self.fail[1], os.strerror(self.fail[1]))
            return self._fsync(fd)

        def publish(staged, target, *args):
            result = self._publish(staged, target, *args)
            self.events.append({"kind": "publish", "id": result})
            return result

        def fake_syncfs(fd):
            event = {"kind": "syncfs", "id": None, "dir": True,
                     "path": os.readlink(f"/proc/self/fd/{fd}"),
                     "snapshot": snapshot_staging(fd)}
            self.events.append(event)
            if self.fail and self.fail[0](event, self.events):
                raise OSError(self.fail[1], os.strerror(self.fail[1]))
            return self._syncfs(fd)

        main.os.fsync = fsync
        main.publish_without_overwrite = publish
        main.syncfs = fake_syncfs
        return self

    def __exit__(self, *exc):
        main.os.fsync = self._fsync
        main.publish_without_overwrite = self._publish
        main.syncfs = self._syncfs
        return False

    def index_of_syncfs(self):
        for i, e in enumerate(self.events):
            if e["kind"] == "syncfs":
                return i
        return None

    def index_of_publish(self):
        for i, e in enumerate(self.events):
            if e["kind"] == "publish":
                return i
        return None

    def fsyncs_of(self, identity):
        return [i for i, e in enumerate(self.events)
                if e["kind"] == "fsync" and e["id"] == identity]


def run_worker(source, destination_parent):
    worker = main.CopyWorker(source, destination_parent, True)
    completed, failed, logs = [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)
    worker.run()
    return worker, completed, failed, logs


def ident(path):
    st = os.stat(path, follow_symlinks=False)
    return (st.st_dev, st.st_ino)


def make_tree(src):
    (src / "sub" / "deep").mkdir(parents=True)
    files = [src / "a.txt", src / "sub" / "b.txt", src / "sub" / "deep" / "c.txt"]
    for i, f in enumerate(files):
        f.write_text(f"data{i}" * 1000)
        os.chmod(f, 0o640)
        os.utime(f, ns=(MTIME_NS, MTIME_NS))
    os.symlink("a.txt", src / "link")
    return files


def test_tree(base):
    print("\n--- A. フォルダーのコピー ---")
    src = base / "a_src"
    files = make_tree(src)
    dst = base / "a_dst"
    dst.mkdir()
    with Recorder() as rec:
        worker, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "コピーが完了した")
    out = dst / "a_src"
    pub = rec.index_of_publish()
    check(pub is not None, "公開処理が記録された")

    for f in files:
        copied = out / f.relative_to(src)
        idx = rec.fsyncs_of(ident(copied))
        check(idx and all(i < pub for i in idx), f"{copied.name}: 公開前に fsync された")
        if idx:
            e = rec.events[idx[-1]]
            check(e["mode"] == 0o640 and e["mtime_ns"] == MTIME_NS,
                  f"{copied.name}: 日時・権限の設定後に fsync された")

    order = {}
    for d in (out / "sub" / "deep", out / "sub", out):
        idx = [i for i in rec.fsyncs_of(ident(d)) if i < pub]
        check(bool(idx), f"フォルダー {d.relative_to(dst)}: 公開前に fsync された")
        order[d.name] = idx[0] if idx else None
    check(None not in order.values()
          and order["deep"] < order["sub"] < order["a_src"],
          "フォルダーは深い階層から順に fsync された")

    after = [i for i in rec.fsyncs_of(ident(dst)) if i > pub]
    check(bool(after), "公開後に、公開先の親フォルダーが fsync された")
    check(not worker.durability_degraded, "書き込み確定は完全に行われた")
    check("ディスクへの書き込み: 確定済み" in (completed[0] if completed else ""),
          "完了メッセージに「確定済み」と表示された")


def test_single_file(base):
    print("\n--- B. ファイル1つのコピー ---")
    src = base / "b_file.txt"
    src.write_text("single" * 1000)
    dst = base / "b_dst"
    dst.mkdir()
    with Recorder() as rec:
        _, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "コピーが完了した")
    pub = rec.index_of_publish()
    f_idx = rec.fsyncs_of(ident(dst / src.name))
    p_idx = rec.fsyncs_of(ident(dst))
    check(f_idx and pub is not None and f_idx[0] < pub, "ファイルは公開前に fsync された")
    check(any(i > pub for i in p_idx), "公開後に親フォルダーが fsync された")


def test_unsupported(base):
    print("\n--- C. fsync 非対応（EINVAL）のファイルシステム ---")
    src = base / "c_src"
    make_tree(src)
    dst = base / "c_dst"
    dst.mkdir()
    with Recorder(fail=(lambda e, _: e["dir"], errno.EINVAL)):
        worker, completed, failed, logs = run_worker(src, dst)
    check(completed and not failed, "コピーは完了した")
    check(worker.durability_degraded, "書き込み確定が不完全であることを記録した")
    check(sum("fsync" in m and "警告" in m for m in logs) == 1, "警告は1回だけ出た")
    check("一部未確定" in (completed[0] if completed else ""),
          "完了メッセージに「一部未確定」と表示された")


def test_eio_before_publish(base):
    print("\n--- D. 公開前のフォルダーの fsync で入出力エラー ---")
    src = base / "d_src"
    make_tree(src)
    dst = base / "d_dst"
    dst.mkdir()
    with Recorder(fail=(lambda e, _: e["dir"] and e["path"].endswith("/sub"),
                        errno.EIO)) as rec:
        _, completed, failed, _ = run_worker(src, dst)
    check(failed and not completed, "コピーはエラーで停止した")
    check(rec.index_of_publish() is None, "公開処理は行われなかった")
    check(not (dst / "d_src").exists(), "コピー先に何も公開されていない")
    check(not [p for p in dst.iterdir() if p.name.startswith(main.STAGING_PREFIX)],
          "一時コピー領域が残っていない")


def test_eio_after_publish(base):
    print("\n--- E. 公開後の親フォルダーの fsync で入出力エラー ---")
    src = base / "e_src"
    make_tree(src)
    dst = base / "e_dst"
    dst.mkdir()
    parent_id = ident(dst)
    with Recorder(fail=(lambda e, events: e["id"] == parent_id
                        and any(x["kind"] == "publish" for x in events),
                        errno.EIO)):
        _, completed, failed, _ = run_worker(src, dst)
    check(failed and not completed, "エラーとして報告された")
    check(any("公開済み" in m and "書き込み確定に失敗" in m for m in failed),
          "「公開済みだが書き込み確定に失敗」と分かる内容だった")
    check((dst / "e_src" / "a.txt").exists(), "公開されたコピーはそのまま残っている")


def test_batch_tree(base):
    print("\n--- F. 一括モード：フォルダーのコピー ---")
    src = base / "f_src"
    files = make_tree(src)
    dst = base / "f_dst"
    dst.mkdir()
    with Recorder() as rec:
        worker, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "コピーが完了した")
    pub = rec.index_of_publish()
    sync = rec.index_of_syncfs()
    check(sync is not None and pub is not None and sync < pub,
          "公開前に syncfs で一括確定した")
    file_fsyncs = [e for e in rec.events[:pub or 0]
                   if e["kind"] == "fsync" and not e["dir"]]
    check(not file_fsyncs, "ファイルごとの fsync は行わなかった")
    if sync is not None:
        snap = rec.events[sync]["snapshot"]
        for f in files:
            key = "f_src/" + "/".join(f.relative_to(src).parts)
            check(snap.get(key) == (0o640, MTIME_NS),
                  f"{f.name}: syncfs の時点で中身・日時・権限が設定済み")
        for d in (src / "sub", src / "sub" / "deep"):
            st = d.stat()
            key = "f_src/" + "/".join(d.relative_to(src).parts)
            check(snap.get(key) == (stat.S_IMODE(st.st_mode), st.st_mtime_ns),
                  f"フォルダー {key}: syncfs の時点で属性が設定済み")
    after = [i for i in rec.fsyncs_of(ident(dst)) if pub is not None and i > pub]
    check(bool(after), "公開後に、公開先の親フォルダーが fsync された")
    root_after = [i for i in rec.fsyncs_of(ident(dst / "f_src"))
                  if pub is not None and i > pub]
    check(bool(root_after), "公開後に、属性を設定したルートフォルダーが fsync された")
    check("確定済み" in (completed[0] if completed else ""), "完了メッセージに「確定済み」")


def test_batch_single_file(base):
    print("\n--- G. 一括モード：ファイル1つのコピー ---")
    src = base / "g_file.txt"
    src.write_text("single" * 1000)
    dst = base / "g_dst"
    dst.mkdir()
    with Recorder() as rec:
        _, completed, failed, _ = run_worker(src, dst)
    pub = rec.index_of_publish()
    sync = rec.index_of_syncfs()
    check(completed and sync is not None and pub is not None and sync < pub,
          "公開前に syncfs で確定した")
    check(any(i > pub for i in rec.fsyncs_of(ident(dst))),
          "公開後に親フォルダーが fsync された")


def test_batch_eio(base):
    print("\n--- H. 一括モード：syncfs で入出力エラー ---")
    src = base / "h_src"
    make_tree(src)
    dst = base / "h_dst"
    dst.mkdir()
    with Recorder(fail=(lambda e, _: e["kind"] == "syncfs", errno.EIO)) as rec:
        _, completed, failed, _ = run_worker(src, dst)
    check(failed and not completed, "コピーはエラーで停止した")
    check(rec.index_of_publish() is None, "公開処理は行われなかった")
    check(not (dst / "h_src").exists(), "コピー先に何も公開されていない")
    check(not [p for p in dst.iterdir() if p.name.startswith(main.STAGING_PREFIX)],
          "一時コピー領域が残っていない")


def test_batch_unsupported(base):
    print("\n--- I. 一括モード：syncfs が使えない ---")
    src = base / "i_src"
    make_tree(src)
    dst = base / "i_dst"
    dst.mkdir()
    with Recorder(fail=(lambda e, _: e["kind"] == "syncfs", errno.ENOSYS)):
        worker, completed, failed, logs = run_worker(src, dst)
    check(completed and not failed, "コピーは完了した")
    check(worker.durability_degraded and "一部未確定" in (completed[0] if completed else ""),
          "「一部未確定」と表示された")


def test_auto_mode():
    print("\n--- J. 自動判定 ---")
    expected = main.kernel_version() >= main.SYNCFS_MIN_KERNEL
    check(main.syncfs_available() == expected,
          f"Linux {'.'.join(map(str, main.kernel_version()))} では"
          f"{'一括' if expected else '個別'}モードを選ぶ")


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 9（フォルダーの fsync）確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="directory_fsync_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    original_mode = main.CopyWorker.BATCH_SYNC
    try:
        print("\n==== 個別モード（ファイル・フォルダーごとに fsync） ====")
        main.CopyWorker.BATCH_SYNC = False
        test_tree(base)
        test_single_file(base)
        test_unsupported(base)
        test_eio_before_publish(base)
        test_eio_after_publish(base)
        print("\n==== 一括モード（公開前に syncfs） ====")
        main.CopyWorker.BATCH_SYNC = True
        test_batch_tree(base)
        test_batch_single_file(base)
        test_batch_eio(base)
        test_batch_unsupported(base)
        batch_base = base / "batch"
        batch_base.mkdir()
        test_eio_after_publish(batch_base)
        main.CopyWorker.BATCH_SYNC = original_mode
        test_auto_mode()
    finally:
        main.CopyWorker.BATCH_SYNC = original_mode
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
