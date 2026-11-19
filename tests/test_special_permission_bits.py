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
    print("UltraCopy Ver.0.7 特殊アクセス権限保持試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="special_permission_",
        dir=runs_root
    ))

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    modes = {
        "normal_0644.txt": 0o644,
        "executable_0755.sh": 0o755,
        "readonly_0444.txt": 0o444,
        "executable_readonly_0555.sh": 0o555,
        "setuid_4755.sh": 0o4755,
    }

    print()
    print("===== コピー元ファイル作成 =====")

    setup_results = []

    for filename, expected_mode in modes.items():
        path = source / filename
        path.write_text(
            f"UltraCopy permission test: {filename}\n",
            encoding="utf-8"
        )
        os.chmod(path, expected_mode)

        actual_mode = stat.S_IMODE(path.stat().st_mode)

        print(
            f"{filename}: "
            f"設定={oct(expected_mode)} "
            f"実際={oct(actual_mode)}"
        )

        setup_results.append(check(
            actual_mode == expected_mode,
            f"{filename}: コピー元の権限を設定できた"
        ))

    if not all(setup_results):
        print("[中止] コピー元の権限設定が一致しません")
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

    for filename, expected_mode in modes.items():
        original = source / filename
        copied = copied_root / filename

        print()
        print(f"===== {filename} =====")

        if not copied.is_file():
            results.append(check(
                False,
                f"{filename}: コピー先ファイルが存在する"
            ))
            continue

        source_mode = stat.S_IMODE(original.stat().st_mode)
        copied_mode = stat.S_IMODE(copied.stat().st_mode)

        print(f"コピー元Mode: {oct(source_mode)}")
        print(f"コピー先Mode: {oct(copied_mode)}")

        results.append(check(
            copied_mode == expected_mode,
            f"{filename}: アクセス権限が保持された"
        ))

        results.append(check(
            original.read_bytes() == copied.read_bytes(),
            f"{filename}: ファイル内容が一致した"
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
