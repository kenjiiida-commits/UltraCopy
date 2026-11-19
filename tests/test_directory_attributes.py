from pathlib import Path
import os
import stat
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


def set_directory_attributes(path, mode, mtime_ns, xattr_value):
    os.chmod(path, mode)

    os.setxattr(
        path,
        "user.ultracopy_directory_test",
        xattr_value
    )

    subprocess.run(
        ["setfacl", "-m", "u:65534:r-x", str(path)],
        check=True,
        capture_output=True,
        text=True
    )

    os.utime(path, ns=(mtime_ns, mtime_ns))


def compare_directory(source, copied, label, expected_mode,
                      expected_mtime, expected_xattr):
    results = []

    source_stat = source.stat()
    copied_stat = copied.stat()

    source_mode = stat.S_IMODE(source_stat.st_mode)
    copied_mode = stat.S_IMODE(copied_stat.st_mode)

    print()
    print(f"===== {label} =====")
    print(f"コピー元Mode : {oct(source_mode)}")
    print(f"コピー先Mode : {oct(copied_mode)}")
    print(f"コピー元MTime: {source_stat.st_mtime_ns}")
    print(f"コピー先MTime: {copied_stat.st_mtime_ns}")

    results.append(check(
        source_mode == expected_mode == copied_mode,
        f"{label}: アクセス権限が一致した"
    ))

    results.append(check(
        source_stat.st_mtime_ns == expected_mtime
        and copied_stat.st_mtime_ns == expected_mtime,
        f"{label}: MTimeがナノ秒単位で一致した"
    ))

    source_xattr = os.getxattr(
        source,
        "user.ultracopy_directory_test"
    )
    copied_xattr = os.getxattr(
        copied,
        "user.ultracopy_directory_test"
    )

    print(f"xattrコピー元: {source_xattr!r}")
    print(f"xattrコピー先: {copied_xattr!r}")

    results.append(check(
        source_xattr == expected_xattr == copied_xattr,
        f"{label}: xattrが一致した"
    ))

    source_acl = get_acl(source)
    copied_acl = get_acl(copied)

    print("--- コピー元ACL ---")
    print(source_acl)
    print("--- コピー先ACL ---")
    print(copied_acl)

    results.append(check(
        source_acl == copied_acl,
        f"{label}: POSIX ACLが一致した"
    ))

    return results


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ディレクトリ属性自動試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="directory_attributes_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    nested = source / "nested"
    destination_parent = run_dir / "destination"

    nested.mkdir(parents=True)
    destination_parent.mkdir()

    (nested / "test.txt").write_text(
        "UltraCopy directory attribute test\n",
        encoding="utf-8"
    )

    root_mode = 0o750
    nested_mode = 0o750

    root_mtime = 1577836800000000000
    nested_mtime = 1609459200000000000

    root_xattr = b"root_directory_attribute"
    nested_xattr = b"nested_directory_attribute"

    # 子ディレクトリの属性を設定してから、親の属性を設定する
    set_directory_attributes(
        nested, nested_mode, nested_mtime, nested_xattr
    )

    set_directory_attributes(
        source, root_mode, root_mtime, root_xattr
    )

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(source, destination_parent, verify=True)

    completed = []
    failed = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)

    worker.run()

    copied_root = destination_parent / source.name
    copied_nested = copied_root / "nested"

    if not copied_root.is_dir() or not copied_nested.is_dir():
        print("[FAIL] コピー先ディレクトリが存在しません")
        for message in failed:
            print(message)
        return 1

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.extend(compare_directory(
        source,
        copied_root,
        "ルートディレクトリ",
        root_mode,
        root_mtime,
        root_xattr
    ))

    results.extend(compare_directory(
        nested,
        copied_nested,
        "サブディレクトリ",
        nested_mode,
        nested_mtime,
        nested_xattr
    ))

    results.append(check(
        (nested / "test.txt").read_bytes()
        == (copied_nested / "test.txt").read_bytes(),
        "サブディレクトリ内のファイル内容が一致した"
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
