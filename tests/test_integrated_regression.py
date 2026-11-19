from pathlib import Path
import os
import stat
import sys
import tempfile
import traceback
import shutil
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def check(results, condition, description):
    if condition:
        print(f"[PASS] {description}")
        results.append(True)
    else:
        print(f"[FAIL] {description}")
        results.append(False)


def same_inode(path_a, path_b):
    return (
        path_a.stat().st_dev == path_b.stat().st_dev
        and path_a.stat().st_ino == path_b.stat().st_ino
    )


def main_test():
    print("=" * 72)
    print("UltraCopy Ver.0.7 統合回帰試験")
    print("=" * 72)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="integrated_regression_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

    source = run_dir / "source"
    destination_parent = run_dir / "destination"
    outside = run_dir / "outside"

    # --------------------------------------------------
    # コピー元のディレクトリ構造
    # --------------------------------------------------

    data_dir = source / "data"
    scripts_dir = source / "scripts"
    nested_dir = source / "nested" / "deep"
    links_dir = source / "links"
    empty_dir = source / "empty"

    for directory in (
        data_dir,
        scripts_dir,
        nested_dir,
        links_dir,
        empty_dir,
        destination_parent,
        outside,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------
    # 通常ファイルとハードリンク
    # --------------------------------------------------

    original = data_dir / "original.txt"
    hardlink = data_dir / "hardlink.txt"
    independent = data_dir / "independent.txt"

    original.write_text(
        "UltraCopy integrated regression test\n"
        "Hardlink and symlink verification\n",
        encoding="utf-8"
    )

    os.link(original, hardlink)

    independent.write_text(
        original.read_text(encoding="utf-8"),
        encoding="utf-8"
    )

    # --------------------------------------------------
    # 実行可能ファイル
    # --------------------------------------------------

    executable = scripts_dir / "run.sh"

    executable.write_text(
        "#!/bin/sh\n"
        "echo UltraCopy integrated test\n",
        encoding="utf-8"
    )

    os.chmod(executable, 0o755)

    # --------------------------------------------------
    # ネストしたディレクトリのファイル
    # --------------------------------------------------

    nested_file = nested_dir / "nested.txt"

    nested_file.write_text(
        "Nested directory regression test\n",
        encoding="utf-8"
    )

    # --------------------------------------------------
    # 拡張属性
    # --------------------------------------------------

    xattr_supported = False
    xattr_name = "user.ultracopy_integrated"
    xattr_value = b"integrated_attribute_test"

    try:
        os.setxattr(nested_file, xattr_name, xattr_value)
        xattr_supported = (
            os.getxattr(nested_file, xattr_name) == xattr_value
        )
    except (OSError, AttributeError) as exc:
        print(f"[INFO] xattrを利用できません: {exc}")

    # --------------------------------------------------
    # POSIX ACL（setfacl/getfaclが利用できる場合）
    # --------------------------------------------------

    acl_supported = False
    acl_source = nested_file
    acl_source_text = None

    if shutil.which("setfacl") and shutil.which("getfacl"):
        try:
            subprocess.run(
                [
                    "setfacl",
                    "-m",
                    "u:65534:r--",
                    str(acl_source)
                ],
                check=True,
                capture_output=True,
                text=True
            )

            acl_source_text = subprocess.run(
                ["getfacl", "-cp", str(acl_source)],
                check=True,
                capture_output=True,
                text=True
            ).stdout

            acl_supported = True

        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"[INFO] POSIX ACLを設定できません: {exc}")

    # --------------------------------------------------
    # シンボリックリンク
    # --------------------------------------------------

    link_original = links_dir / "link_original"
    link_hardlink = links_dir / "link_hardlink"
    link_chain = links_dir / "link_chain"
    external_link = links_dir / "external_relative"

    os.symlink("../data/original.txt", link_original)
    os.symlink("../data/hardlink.txt", link_hardlink)
    os.symlink("link_original", link_chain)

    outside_file = outside / "outside.txt"
    outside_file.write_text(
        "This file must not be copied.\n",
        encoding="utf-8"
    )

    os.symlink("../../outside/outside.txt", external_link)

    outside_original_content = outside_file.read_bytes()

    # --------------------------------------------------
    # コピー元の事前確認
    # --------------------------------------------------

    print()
    print("===== コピー元の構造 =====")

    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            print(f"[LINK] {path.relative_to(source)} -> {os.readlink(path)}")
        elif path.is_dir():
            print(f"[DIR ] {path.relative_to(source)}")
        else:
            print(f"[FILE] {path.relative_to(source)}")

    print()
    print("===== コピー元ハードリンク情報 =====")
    print(f"original inode : {original.stat().st_ino}")
    print(f"hardlink inode : {hardlink.stat().st_ino}")
    print(f"original nlink : {original.stat().st_nlink}")

    print()
    print("===== 属性設定状況 =====")
    print(f"xattr利用可能: {'はい' if xattr_supported else 'いいえ'}")
    print(f"ACL利用可能  : {'はい' if acl_supported else 'いいえ'}")

    # --------------------------------------------------
    # CopyWorker実行
    # --------------------------------------------------

    print()
    print("===== CopyWorker実行 =====")

    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed = []
    failed = []
    logs = []

    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.log.connect(logs.append)

    try:
        worker.run()
    except Exception as exc:
        print(f"[ERROR] CopyWorker例外: {exc}")
        traceback.print_exc()
        return 1

    destination = destination_parent / source.name

    if not destination.is_dir():
        print("[FAIL] コピー先ディレクトリが存在しません")
        for message in failed:
            print(message)
        return 1

    # --------------------------------------------------
    # コピー先の検証
    # --------------------------------------------------

    results = []

    print()
    print("===== 統合検証結果 =====")

    check(
        results,
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    )

    copied_original = destination / "data" / "original.txt"
    copied_hardlink = destination / "data" / "hardlink.txt"
    copied_independent = destination / "data" / "independent.txt"

    check(
        results,
        copied_original.is_file()
        and copied_hardlink.is_file()
        and copied_independent.is_file(),
        "3つの通常ファイルがコピーされた"
    )

    check(
        results,
        same_inode(original, hardlink),
        "コピー元のハードリンク関係が維持されている"
    )

    check(
        results,
        same_inode(copied_original, copied_hardlink),
        "コピー先でもハードリンク関係が維持された"
    )

    check(
        results,
        not same_inode(copied_original, copied_independent),
        "独立ファイルが別inodeとして維持された"
    )

    check(
        results,
        copied_original.read_bytes() == original.read_bytes()
        and copied_hardlink.read_bytes() == hardlink.read_bytes()
        and copied_independent.read_bytes() == independent.read_bytes(),
        "通常ファイル3つの内容が一致した"
    )

    # --------------------------------------------------
    # シンボリックリンク
    # --------------------------------------------------

    copied_link_original = destination / "links" / "link_original"
    copied_link_hardlink = destination / "links" / "link_hardlink"
    copied_link_chain = destination / "links" / "link_chain"
    copied_external_link = destination / "links" / "external_relative"

    copied_links = (
        copied_link_original,
        copied_link_hardlink,
        copied_link_chain,
        copied_external_link,
    )

    check(
        results,
        all(path.is_symlink() for path in copied_links),
        "4つのシンボリックリンクがリンクとして複製された"
    )

    expected_targets = (
        "../data/original.txt",
        "../data/hardlink.txt",
        "link_original",
        "../../outside/outside.txt",
    )

    check(
        results,
        all(
            os.readlink(path) == expected
            for path, expected in zip(copied_links, expected_targets)
            if path.is_symlink()
        )
        and all(path.is_symlink() for path in copied_links),
        "4つのリンク先文字列が一致した"
    )

    check(
        results,
        copied_link_chain.is_symlink()
        and copied_link_chain.read_text(encoding="utf-8")
        == original.read_text(encoding="utf-8"),
        "リンク連鎖を通じてコピー先データを読み取れる"
    )

    check(
        results,
        copied_link_original.is_symlink()
        and copied_link_hardlink.is_symlink()
        and same_inode(copied_link_original, copied_link_hardlink),
        "2つのリンクが同じハードリンク実体を参照する"
    )

    check(
        results,
        copied_external_link.is_symlink()
        and not copied_external_link.exists()
        and not (destination_parent / "outside").exists(),
        "外部リンク先の実体をコピー先へ複製していない"
    )

    # --------------------------------------------------
    # ディレクトリ構造
    # --------------------------------------------------

    copied_nested_dir = destination / "nested" / "deep"
    copied_empty_dir = destination / "empty"

    check(
        results,
        copied_nested_dir.is_dir(),
        "ネストしたディレクトリ構造が維持された"
    )

    check(
        results,
        copied_empty_dir.is_dir()
        and not any(copied_empty_dir.iterdir()),
        "空ディレクトリが空のままコピーされた"
    )

    # --------------------------------------------------
    # 特殊アクセス権限
    # --------------------------------------------------

    copied_executable = destination / "scripts" / "run.sh"

    check(
        results,
        stat.S_IMODE(executable.stat().st_mode) == 0o755
        and stat.S_IMODE(copied_executable.stat().st_mode) == 0o755,
        "実行ファイルのアクセス権限0755が保持された"
    )

    # --------------------------------------------------
    # 拡張属性
    # --------------------------------------------------

    if xattr_supported:
        try:
            copied_xattr = os.getxattr(
                destination / "nested" / "deep" / "nested.txt",
                xattr_name
            )
            xattr_ok = copied_xattr == xattr_value
        except OSError:
            xattr_ok = False

        check(
            results,
            xattr_ok,
            "拡張属性xattrが保持された"
        )
    else:
        print("[SKIP] xattrはこの環境で利用できません")

    # --------------------------------------------------
    # POSIX ACL
    # --------------------------------------------------

    if acl_supported:
        try:
            copied_acl_text = subprocess.run(
                [
                    "getfacl",
                    "-cp",
                    str(destination / "nested" / "deep" / "nested.txt")
                ],
                check=True,
                capture_output=True,
                text=True
            ).stdout

            acl_ok = copied_acl_text == acl_source_text

        except (OSError, subprocess.CalledProcessError):
            acl_ok = False

        check(
            results,
            acl_ok,
            "POSIX ACLが保持された"
        )
    else:
        print("[SKIP] POSIX ACLはこの環境で利用できません")

    # --------------------------------------------------
    # コピー元への影響確認
    # --------------------------------------------------

    check(
        results,
        outside_file.read_bytes() == outside_original_content,
        "コピー対象外の外部ファイルが変更されていない"
    )

    check(
        results,
        same_inode(original, hardlink)
        and original.read_bytes() == hardlink.read_bytes(),
        "コピー元のハードリンク関係と内容が維持されている"
    )

    # --------------------------------------------------
    # 最終結果
    # --------------------------------------------------

    passed = sum(results)
    total = len(results)

    print()
    print("=" * 72)
    print(f"統合試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 72)

    if failed:
        print()
        print("===== CopyWorkerエラー =====")
        for message in failed:
            print(message)

    if not all(results):
        print()
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
