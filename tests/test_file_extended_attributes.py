from pathlib import Path
import os
import sys
import tempfile
import subprocess
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


def get_acl(path):
    result = subprocess.run(
        ["getfacl", "-cp", str(path)],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 xattr / POSIX ACL 自動試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="extended_attributes_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"

    source.mkdir()
    destination_parent.mkdir()

    source_file = source / "extended_test.txt"
    source_file.write_text(
        "UltraCopy xattr and ACL test\n",
        encoding="utf-8"
    )

    results = []

    print()
    print("===== xattr設定 =====")

    xattr_name = "user.ultracopy_test"
    xattr_value = b"UltraCopy_attribute_test_2026"

    xattr_supported = False
    source_xattr = None

    try:
        os.setxattr(source_file, xattr_name, xattr_value)
        source_xattr = os.getxattr(source_file, xattr_name)
        xattr_supported = True
        print(f"設定値: {source_xattr!r}")
    except OSError as exc:
        print(f"[SKIP] xattrを設定できません: {exc}")

    print()
    print("===== POSIX ACL設定 =====")

    acl_supported = False
    source_acl = None

    try:
        subprocess.run(
            ["setfacl", "-m", "u:65534:r--", str(source_file)],
            check=True,
            capture_output=True,
            text=True
        )
        source_acl = get_acl(source_file)
        acl_supported = True
        print(source_acl)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[SKIP] ACLを設定できません: {exc}")

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed = []
    failed = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)

    worker.run()

    copied_file = destination_parent / source.name / source_file.name

    if not copied_file.is_file():
        print("[FAIL] コピー先ファイルが存在しません")
        for message in failed:
            print(message)
        return 1

    print()
    print("===== xattr比較 =====")

    if xattr_supported:
        try:
            copied_xattr = os.getxattr(copied_file, xattr_name)
            print(f"コピー元: {source_xattr!r}")
            print(f"コピー先: {copied_xattr!r}")
            results.append(check(
                source_xattr == copied_xattr,
                "user.ultracopy_test が一致した"
            ))
        except OSError as exc:
            print(f"[FAIL] コピー先xattrを取得できません: {exc}")
            results.append(False)
    else:
        print("[SKIP] xattr試験は環境上実施できません")

    print()
    print("===== POSIX ACL比較 =====")

    if acl_supported:
        try:
            copied_acl = get_acl(copied_file)
            print("--- コピー元 ---")
            print(source_acl)
            print("--- コピー先 ---")
            print(copied_acl)

            results.append(check(
                source_acl == copied_acl,
                "POSIX ACLが一致した"
            ))
        except Exception as exc:
            print(f"[FAIL] コピー先ACLを取得できません: {exc}")
            results.append(False)
    else:
        print("[SKIP] ACL試験は環境上実施できません")

    print()
    print("===== コピー結果 =====")

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.append(check(
        source_file.read_bytes() == copied_file.read_bytes(),
        "ファイル内容が一致した"
    ))

    print()
    print("=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"実施項目結果: {passed} / {total} PASS")
    print(f"xattr利用可能: {'はい' if xattr_supported else 'いいえ'}")
    print(f"ACL利用可能  : {'はい' if acl_supported else 'いいえ'}")
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
