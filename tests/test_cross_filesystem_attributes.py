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


def get_filesystem(path):
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)],
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout.strip()


def set_attributes(path, mode, mtime_ns, xattr_value, acl):
    os.chmod(path, mode)

    os.setxattr(
        path,
        "user.ultracopy_cross_fs_test",
        xattr_value
    )

    subprocess.run(
        ["setfacl", "-m", acl, str(path)],
        check=True,
        capture_output=True,
        text=True
    )

    os.utime(path, ns=(mtime_ns, mtime_ns))


def compare_attributes(source, copied, label, expected_mode,
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
        f"{label}: MTimeが一致した"
    ))

    source_xattr = os.getxattr(
        source, "user.ultracopy_cross_fs_test"
    )
    copied_xattr = os.getxattr(
        copied, "user.ultracopy_cross_fs_test"
    )

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
    print("UltraCopy Ver.0.7 異なるファイルシステム間の属性保持試験")
    print("=" * 70)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    ext4_run = Path(tempfile.mkdtemp(
        prefix="cross_fs_ext4_",
        dir=runs_root
    ))

    tmpfs_run = Path(tempfile.mkdtemp(
        prefix="ultracopy_cross_fs_",
        dir="/dev/shm"
    ))

    print(f"ext4試験場所 : {ext4_run}")
    print(f"tmpfs試験場所: {tmpfs_run}")

    source = ext4_run / "source"
    nested = source / "nested"
    source_file = nested / "test.txt"

    destination_parent = tmpfs_run / "destination"

    nested.mkdir(parents=True)
    destination_parent.mkdir()

    main.DATA_DIR = ext4_run / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    print()
    print("===== ファイルシステム確認 =====")

    source_fs = get_filesystem(source)
    destination_fs = get_filesystem(destination_parent)

    print(f"コピー元 : {source_fs}")
    print(f"コピー先 : {destination_fs}")

    if source_fs == destination_fs:
        print("[FAIL] コピー元とコピー先のFSが異なりません")
        return 1

    root_mode = 0o750
    nested_mode = 0o750
    file_mode = 0o640

    root_mtime = 1577836800000000000
    nested_mtime = 1609459200000000000
    file_mtime = 1640995200000000000

    root_xattr = b"cross_fs_root"
    nested_xattr = b"cross_fs_nested"
    file_xattr = b"cross_fs_file"

    source_file.write_text(
        "UltraCopy cross filesystem attribute test\n",
        encoding="utf-8"
    )

    set_attributes(
        source_file,
        file_mode,
        file_mtime,
        file_xattr,
        "u:65534:r--"
    )

    set_attributes(
        nested,
        nested_mode,
        nested_mtime,
        nested_xattr,
        "u:65534:r-x"
    )

    set_attributes(
        source,
        root_mode,
        root_mtime,
        root_xattr,
        "u:65534:r-x"
    )

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
    copied_nested = copied_root / "nested"
    copied_file = copied_nested / "test.txt"

    if not copied_file.is_file():
        print("[FAIL] コピー先ファイルが存在しません")
        for message in failed:
            print(message)
        return 1

    results = []

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    results.extend(compare_attributes(
        source,
        copied_root,
        "ルートディレクトリ",
        root_mode,
        root_mtime,
        root_xattr
    ))

    results.extend(compare_attributes(
        nested,
        copied_nested,
        "サブディレクトリ",
        nested_mode,
        nested_mtime,
        nested_xattr
    ))

    results.extend(compare_attributes(
        source_file,
        copied_file,
        "通常ファイル",
        file_mode,
        file_mtime,
        file_xattr
    ))

    results.append(check(
        source_file.read_bytes() == copied_file.read_bytes(),
        "ファイル内容が一致した"
    ))

    print()
    print("=" * 70)
    print(f"試験結果: {sum(results)} / {len(results)} 項目 PASS")
    print(f"ext4試験データ : {ext4_run}")
    print(f"tmpfs試験データ: {tmpfs_run}")
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
