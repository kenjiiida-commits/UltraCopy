from pathlib import Path
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def run_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 コピー対象外ハードリンク試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    test_root = Path(tempfile.mkdtemp(
        prefix="hardlink_external_",
        dir=runs_root
    ))

    print(f"試験作業場所: {test_root}")

    source = test_root / "source"
    source.mkdir()

    outside_file = test_root / "outside.txt"
    inside_a = source / "inside_A.txt"
    inside_b = source / "inside_B.txt"

    content = (
        "UltraCopy external hardlink test\n"
        "コピー対象外のハードリンクを検証します。\n"
    ).encode("utf-8")

    outside_file.write_bytes(content)
    os.link(outside_file, inside_a)
    os.link(inside_a, inside_b)

    destination_parent = test_root / "destination"
    destination_parent.mkdir()

    destination = destination_parent / source.name

    main.DATA_DIR = test_root / "data"
    main.DATA_DIR.mkdir()
    main.DB_PATH = main.DATA_DIR / "history.db"

    results = []
    errors = []
    completed_events = []

    def check(condition, description):
        if condition:
            print(f"[PASS] {description}")
            results.append(True)
        else:
            print(f"[FAIL] {description}")
            results.append(False)

    print("\n===== コピー元のinode確認 =====")

    source_paths = [outside_file, inside_a, inside_b]

    for path in source_paths:
        info = path.stat()
        print(
            f"{path.relative_to(test_root)}: "
            f"device={info.st_dev}, inode={info.st_ino}, "
            f"nlink={info.st_nlink}"
        )

    source_stats = [path.stat() for path in source_paths]

    check(
        len({(s.st_dev, s.st_ino) for s in source_stats}) == 1,
        "外部ファイルとコピー対象2ファイルが同じinodeを共有している"
    )

    check(
        all(s.st_nlink == 3 for s in source_stats),
        "コピー元のハードリンク数が3である"
    )

    original_outside_inode = outside_file.stat().st_ino
    original_outside_content = outside_file.read_bytes()

    print("\n===== CopyWorker実行 =====")

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

        worker.run()

    except Exception:
        print("[ERROR] CopyWorker実行中に例外が発生しました")
        traceback.print_exc()
        return

    print("\n===== コピー先の確認 =====")

    dest_a = destination / "inside_A.txt"
    dest_b = destination / "inside_B.txt"
    dest_outside = destination / "outside.txt"

    check(
        dest_a.is_file() and dest_b.is_file(),
        "コピー対象の2ファイルがコピー先に存在する"
    )

    if dest_a.exists() and dest_b.exists():
        info_a = dest_a.stat()
        info_b = dest_b.stat()

        print(
            f"inside_A.txt: device={info_a.st_dev}, "
            f"inode={info_a.st_ino}, nlink={info_a.st_nlink}"
        )
        print(
            f"inside_B.txt: device={info_b.st_dev}, "
            f"inode={info_b.st_ino}, nlink={info_b.st_nlink}"
        )

        check(
            (info_a.st_dev, info_a.st_ino)
            == (info_b.st_dev, info_b.st_ino),
            "コピー先の2ファイルが同じinodeを共有している"
        )

        check(
            info_a.st_nlink == 2 and info_b.st_nlink == 2,
            "コピー先のハードリンク数が2である"
        )
    else:
        check(False, "コピー先の2ファイルのinodeを確認できる")

    check(
        not dest_outside.exists(),
        "コピー対象外のoutside.txtがコピー先に作成されていない"
    )

    check(
        outside_file.exists()
        and outside_file.stat().st_ino == original_outside_inode
        and outside_file.read_bytes() == original_outside_content,
        "コピー元ディレクトリ外のファイルが変更されていない"
    )

    check(
        dest_a.exists()
        and dest_b.exists()
        and dest_a.read_bytes() == content
        and dest_b.read_bytes() == content,
        "コピー先2ファイルの内容が正しい"
    )

    check(
        not errors,
        "CopyWorkerから失敗シグナルが発生していない"
    )

    check(
        bool(completed_events),
        "CopyWorkerの完了シグナルを受信した"
    )

    print("\n===== 試験結果 =====")

    passed = sum(
        1 for item in results
        if item is True
    )
    total = len(results)

    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {test_root}")
    print("=" * 70)


if __name__ == "__main__":
    run_test()
