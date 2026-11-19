from pathlib import Path
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def check(condition, description):
    if condition:
        print(f"[PASS] {description}")
        return True
    print(f"[FAIL] {description}")
    return False


def same_inode(path_a, path_b):
    stat_a = path_a.stat()
    stat_b = path_b.stat()
    return (
        stat_a.st_dev == stat_b.st_dev
        and stat_a.st_ino == stat_b.st_ino
    )


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ハードリンク応用試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="hardlink_advanced_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    group_a = source / "group_A"
    group_b = source / "group_B"

    group_a.mkdir(parents=True)
    group_b.mkdir(parents=True)
    destination_parent.mkdir()

    file_1 = group_a / "file_1.txt"
    file_2 = group_a / "file_2.txt"
    file_3 = group_b / "file_3.txt"
    independent = source / "independent.txt"

    content = "UltraCopy advanced hardlink test\n"

    file_1.write_text(content, encoding="utf-8")

    # 3つのパスを同じinodeに結び付ける
    os.link(file_1, file_2)
    os.link(file_1, file_3)

    # 内容は同じだが、別inodeの独立ファイル
    independent.write_text(content, encoding="utf-8")

    source_files = [file_1, file_2, file_3]

    print()
    print("===== コピー元のinode確認 =====")

    for path in source_files + [independent]:
        info = path.stat()
        print(
            f"{path.relative_to(source)}: "
            f"device={info.st_dev}, inode={info.st_ino}, "
            f"nlink={info.st_nlink}"
        )

    source_hardlinks_valid = (
        same_inode(file_1, file_2)
        and same_inode(file_1, file_3)
        and all(path.stat().st_nlink >= 3 for path in source_files)
    )

    source_independent_valid = (
        not same_inode(file_1, independent)
    )

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    worker.run()

    copied_root = destination_parent / source.name

    copied_file_1 = copied_root / "group_A" / "file_1.txt"
    copied_file_2 = copied_root / "group_A" / "file_2.txt"
    copied_file_3 = copied_root / "group_B" / "file_3.txt"
    copied_independent = copied_root / "independent.txt"

    print()
    print("===== コピー先のinode確認 =====")

    required_paths = [
        copied_file_1,
        copied_file_2,
        copied_file_3,
        copied_independent,
    ]

    missing = [path for path in required_paths if not path.is_file()]

    if missing:
        print("[FAIL] コピー先のファイルが不足しています")
        for path in missing:
            print(f"  不足: {path}")
        for message in failed:
            print(message)
        return 1

    for path in required_paths:
        info = path.stat()
        print(
            f"{path.relative_to(copied_root)}: "
            f"device={info.st_dev}, inode={info.st_ino}, "
            f"nlink={info.st_nlink}"
        )

    destination_hardlinks_valid = (
        same_inode(copied_file_1, copied_file_2)
        and same_inode(copied_file_1, copied_file_3)
    )

    destination_independent_valid = (
        not same_inode(copied_file_1, copied_independent)
    )

    contents_valid = all(
        destination.read_bytes() == original.read_bytes()
        for original, destination in [
            (file_1, copied_file_1),
            (file_2, copied_file_2),
            (file_3, copied_file_3),
            (independent, copied_independent),
        ]
    )

    print()
    print("===== 試験結果 =====")

    results = []

    results.append(check(
        source_hardlinks_valid,
        "コピー元の3ファイルが同じinodeを共有している"
    ))

    results.append(check(
        source_independent_valid,
        "コピー元の独立ファイルは別inodeである"
    ))

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.append(check(
        contents_valid,
        "4ファイルすべての内容がコピー元と一致した"
    ))

    results.append(check(
        destination_hardlinks_valid,
        "異なるサブディレクトリ間でも3ファイルのハードリンクが維持された"
    ))

    results.append(check(
        destination_independent_valid,
        "独立ファイルが誤ってハードリンク化されていない"
    ))

    print()
    print("=" * 70)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 70)

    if failed:
        print("===== エラー =====")
        for message in failed:
            print(message)

    if not all(results):
        print("===== CopyWorkerログ =====")
        for message in logs:
            print(message)
        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
