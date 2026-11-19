"""レビュー指摘 6：スキップした特殊ファイルを完了メッセージと履歴に表示する。

A. FIFO・ソケット（root ならデバイスも）を含むコピー
   → 完了メッセージと履歴に件数・パス・種類が出て、結果は「成功（スキップあり）」
B. 特殊ファイルがない場合は、従来どおり「成功」でスキップの記載なし
C. 多数スキップした場合、完了メッセージは上限件数＋「ほか N 件」、履歴には全件
D. スキップの後にエラーで停止した場合も、履歴にスキップした項目が残る
"""
from pathlib import Path
import errno
import os
import shutil
import socket
import sqlite3
import stat
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


def last_history():
    with sqlite3.connect(main.DB_PATH) as db:
        return db.execute(
            "SELECT result, details FROM copy_history ORDER BY id DESC LIMIT 1"
        ).fetchone()


def make_socket(path):
    # ソケットのパス長制限（108バイト）を避けるため、相対パスで作成する。
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path.name)
        sock.close()
    finally:
        os.chdir(cwd)


def test_skips_reported(base):
    print("\n--- A. 特殊ファイルを含むコピー ---")
    src = base / "a_src"
    (src / "sub").mkdir(parents=True)
    (src / "normal.txt").write_text("normal")
    (src / "sub" / "inner.txt").write_text("inner")
    os.mkfifo(src / "pipe")
    make_socket(src / "sub" / "sock")
    expected = {"pipe": "名前付きパイプ（FIFO）", "sock": "ソケット"}
    if os.geteuid() == 0:
        os.mknod(src / "chardev", stat.S_IFCHR | 0o600, os.makedev(1, 3))
        expected["chardev"] = "キャラクターデバイス"
    else:
        print("[INFO] 一般ユーザーのため、デバイスファイルの確認は省略します。")
    dst = base / "a_dst"
    dst.mkdir()

    worker, completed, failed, _ = run_worker(src, dst)
    check(completed and not failed, "コピーは完了した")
    message = completed[0] if completed else ""
    check(f"コピーしなかった項目が {len(expected)} 件" in message,
          f"完了メッセージにスキップ件数（{len(expected)} 件）が出た")
    for name, kind in expected.items():
        check(name in message and kind in message,
              f"完了メッセージに {name} と種類「{kind}」が出た")
    check(len(worker.skipped_items) == len(expected), "スキップ一覧の件数が正しい")

    result, details = last_history()
    check(result == main.RESULT_SUCCESS_WITH_SKIPS, f"履歴の結果が「{result}」")
    for name in expected:
        check(name in details, f"履歴の詳細に {name} が記録された")

    out = dst / "a_src"
    check((out / "normal.txt").read_text() == "normal"
          and (out / "sub" / "inner.txt").read_text() == "inner",
          "通常ファイルは正しくコピーされた")
    check(not any((out / n).exists() for n in ("pipe", "chardev"))
          and not (out / "sub" / "sock").exists(),
          "特殊ファイルはコピー先に作られていない")


def test_no_skips(base):
    print("\n--- B. 特殊ファイルがない場合 ---")
    src = base / "b_src"
    src.mkdir()
    (src / "f.txt").write_text("x")
    dst = base / "b_dst"
    dst.mkdir()
    worker, completed, failed, _ = run_worker(src, dst)
    message = completed[0] if completed else ""
    check(completed and "スキップ" not in message, "完了メッセージにスキップの記載がない")
    result, _ = last_history()
    check(result == main.RESULT_SUCCESS, f"履歴の結果が「{result}」")


def test_many_skips(base):
    print("\n--- C. 多数の特殊ファイル ---")
    src = base / "c_src"
    src.mkdir()
    (src / "f.txt").write_text("x")
    total = main.SKIP_MESSAGE_LIMIT + 5
    for i in range(total):
        os.mkfifo(src / f"pipe{i:02d}")
    dst = base / "c_dst"
    dst.mkdir()
    worker, completed, failed, _ = run_worker(src, dst)
    message = completed[0] if completed else ""
    check(message.count("・") == main.SKIP_MESSAGE_LIMIT + 1,
          f"完了メッセージは {main.SKIP_MESSAGE_LIMIT} 件＋「ほか」の行に収まった")
    check("ほか 5 件" in message, "残りの件数（ほか 5 件）が示された")
    _, details = last_history()
    check(all(f"pipe{i:02d}" in details for i in range(total)),
          f"履歴には全 {total} 件が記録された")


def test_skip_then_error(base):
    print("\n--- D. スキップの後にエラーで停止 ---")
    src = base / "d_src"
    src.mkdir()
    os.mkfifo(src / "a_pipe")
    (src / "z_file.txt").write_text("x")
    dst = base / "d_dst"
    dst.mkdir()

    def inject(worker):
        def failing_copy_stream(s, d):
            raise OSError(errno.EIO, "Input/output error (simulated)", str(s))
        worker._copy_stream = failing_copy_stream

    worker, completed, failed, _ = run_worker(src, dst, inject)
    check(failed and not completed, "コピーはエラーで停止した")
    result, details = last_history()
    check(result == "エラー", f"履歴の結果が「{result}」")
    check("a_pipe" in details and "停止までにスキップ" in details,
          "履歴の詳細にスキップした項目が残った")


def main_test():
    print("=" * 72)
    print("UltraCopy レビュー指摘 6（スキップした特殊ファイルの表示）確認テスト")
    print("=" * 72)
    runs = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="skipped_special_", dir=runs))
    main.DATA_DIR = base / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"
    try:
        test_skips_reported(base)
        test_no_skips(base)
        test_many_skips(base)
        test_skip_then_error(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 72)
    print(f"結果: {sum(RESULTS)}/{len(RESULTS)} PASS")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main_test())
