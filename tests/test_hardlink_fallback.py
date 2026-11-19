from pathlib import Path
import errno
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def run_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ハードリンク失敗時フォールバック試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    test_root = Path(tempfile.mkdtemp(
        prefix="hardlink_fallback_",
        dir=runs_root
    ))

    print(f"試験作業場所: {test_root}")

    source = test_root / "source"
    source.mkdir()

    file_a = source / "file_A.txt"
    file_b = source / "file_B.txt"

    content = (
        "UltraCopy hardlink fallback test\n"
        "ハードリンク失敗時の通常コピーを検証します。\n"
    ).encode("utf-8")

    file_a.write_bytes(content)
    os.link(file_a, file_b)

    destination_parent = test_root / "destination"
    destination_parent.mkdir()

    destination = destination_parent / source.name

    main.DATA_DIR = test_root / "data"
    main.DATA_DIR.mkdir()
    main.DB_PATH = main.DATA_DIR / "history.db"

    results = []
    errors = []
    completed_events = []
    link_attempts = []

    def check(condition, description):
        if condition:
            print(f"[PASS] {description}")
        else:
            print(f"[FAIL] {description}")
        results.append(bool(condition))

    print("\n===== コピー元の確認 =====")

    info_a = file_a.stat()
    info_b = file_b.stat()

    print(
        f"file_A.txt: inode={info_a.st_ino}, "
        f"nlink={info_a.st_nlink}"
    )
    print(
        f"file_B.txt: inode={info_b.st_ino}, "
        f"nlink={info_b.st_nlink}"
    )

    check(
        (info_a.st_dev, info_a.st_ino)
        == (info_b.st_dev, info_b.st_ino),
        "コピー元2ファイルが同じinodeを共有している"
    )

    check(
        info_a.st_nlink == 2 and info_b.st_nlink == 2,
        "コピー元のハードリンク数が2である"
    )

    print("\n===== ハードリンク作成失敗を模擬 =====")

    original_link = os.link

    def failing_link(src, dst, *args, **kwargs):
        src_path = Path(src)
        dst_path = Path(dst)

        # 2つ目のファイルを作成する際の、
        # 既存コピー先ファイルからのハードリンクだけを失敗させる。
        destination_names = {"file_A.txt", "file_B.txt"}

        if (
            src_path.name in destination_names
            and dst_path.name in destination_names
            and src_path.name != dst_path.name
        ):
            link_attempts.append((str(src_path), str(dst_path)))
            raise OSError(
                errno.EOPNOTSUPP,
                "Simulated hardlink unsupported"
            )

        # 一時ファイルの公開など、その他のリンク作成は許可する。
        return original_link(src, dst, *args, **kwargs)

    try:
        worker = main.CopyWorker(
            source,
            destination_parent,
            verify=True
        )

        worker.completed.connect(
            lambda *args: completed_events.append(args)
        )
        worker.failed.connect(
            lambda *args: errors.append(args)
        )

        os.link = failing_link

        worker.run()

    except Exception:
        print("[ERROR] CopyWorker実行中に例外が発生しました")
        traceback.print_exc()

    finally:
        os.link = original_link

    print("\n===== コピー先の確認 =====")

    dest_a = destination / "file_A.txt"
    dest_b = destination / "file_B.txt"

    check(
        len(link_attempts) >= 1,
        "ハードリンク作成が試みられた"
    )

    check(
        dest_a.is_file() and dest_b.is_file(),
        "コピー先に2ファイルが存在する"
    )

    if dest_a.is_file() and dest_b.is_file():
        dest_info_a = dest_a.stat()
        dest_info_b = dest_b.stat()

        print(
            f"file_A.txt: inode={dest_info_a.st_ino}, "
            f"nlink={dest_info_a.st_nlink}"
        )
        print(
            f"file_B.txt: inode={dest_info_b.st_ino}, "
            f"nlink={dest_info_b.st_nlink}"
        )

        check(
            dest_a.read_bytes() == content
            and dest_b.read_bytes() == content,
            "通常コピーへの切り替え後も内容が正しい"
        )

        check(
            (dest_info_a.st_dev, dest_info_a.st_ino)
            != (dest_info_b.st_dev, dest_info_b.st_ino),
            "フォールバック後は独立した2ファイルになっている"
        )

    else:
        check(False, "コピー先ファイルの内容とinodeを確認できる")
        check(False, "フォールバック後の独立性を確認できる")

    check(
        not errors,
        "CopyWorkerから失敗シグナルが発生していない"
    )

    check(
        bool(completed_events),
        "CopyWorkerの完了シグナルを受信した"
    )

    print("\n===== 試験結果 =====")

    passed = sum(results)
    total = len(results)

    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"ハードリンク試行回数: {len(link_attempts)}")
    print(f"保存場所: {test_root}")
    print("=" * 70)


if __name__ == "__main__":
    run_test()
