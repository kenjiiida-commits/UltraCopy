from pathlib import Path
import os
import stat
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


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ディレクトリ特殊ビット保持試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="special_directory_bits_",
        dir=runs_root
    ))

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    modes = {
        "sticky_directory": 0o1755,
        "sgid_directory": 0o2755,
        "sticky_sgid_directory": 0o3775,
    }

    print()
    print("===== コピー元ディレクトリ作成 =====")

    setup_results = []

    for dirname, expected_mode in modes.items():
        directory = source / dirname
        directory.mkdir()

        child = directory / "test.txt"
        child.write_text(
            f"UltraCopy special directory test: {dirname}\n",
            encoding="utf-8"
        )

        os.chmod(directory, expected_mode)

        actual_mode = stat.S_IMODE(directory.stat().st_mode)

        print(
            f"{dirname}: "
            f"設定={oct(expected_mode)} "
            f"実際={oct(actual_mode)}"
        )

        setup_results.append(check(
            actual_mode == expected_mode,
            f"{dirname}: コピー元の特殊ビットを設定できた"
        ))

    if not all(setup_results):
        print("[中止] コピー元の特殊ビット設定が一致しません")
        return 1

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed = []
    failed = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)

    worker.run()

    copied_root = destination_parent / source.name

    if not copied_root.is_dir():
        print("[FAIL] コピー先ディレクトリがありません")
        for message in failed:
            print(message)
        return 1

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    for dirname, expected_mode in modes.items():
        original = source / dirname
        copied = copied_root / dirname

        print()
        print(f"===== {dirname} =====")

        if not copied.is_dir():
            results.append(check(
                False,
                f"{dirname}: コピー先ディレクトリが存在する"
            ))
            continue

        source_mode = stat.S_IMODE(original.stat().st_mode)
        copied_mode = stat.S_IMODE(copied.stat().st_mode)

        print(f"コピー元Mode: {oct(source_mode)}")
        print(f"コピー先Mode: {oct(copied_mode)}")

        results.append(check(
            source_mode == expected_mode,
            f"{dirname}: コピー元の特殊ビットが維持されている"
        ))

        results.append(check(
            copied_mode == expected_mode,
            f"{dirname}: コピー先に特殊ビットが保持された"
        ))

        original_file = original / "test.txt"
        copied_file = copied / "test.txt"

        results.append(check(
            copied_file.is_file()
            and original_file.read_bytes() == copied_file.read_bytes(),
            f"{dirname}: 内部ファイルが正しくコピーされた"
        ))

    print()
    print("=" * 70)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 70)

    if failed:
        print("===== CopyWorkerエラー =====")
        for message in failed:
            print(message)

    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
