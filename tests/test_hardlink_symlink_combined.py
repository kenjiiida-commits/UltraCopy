from pathlib import Path
import os
import sys
import tempfile

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
    info_a = path_a.stat()
    info_b = path_b.stat()

    return (
        info_a.st_dev == info_b.st_dev
        and info_a.st_ino == info_b.st_ino
    )


def main_test():
    print("=" * 70)
    print("UltraCopy Ver.0.7 ハードリンク・シンボリックリンク複合試験")
    print("=" * 70)

    runs_root = (
        PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    )
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="hardlink_symlink_combined_",
        dir=runs_root
    ))

    print(f"試験作業場所: {run_dir}")

    source = run_dir / "source"
    destination_parent = run_dir / "destination"
    outside = run_dir / "outside"

    data_dir = source / "data"
    links_dir = source / "links"

    data_dir.mkdir(parents=True)
    links_dir.mkdir(parents=True)
    destination_parent.mkdir()
    outside.mkdir()

    # ハードリンク対象ファイル
    original = data_dir / "original.txt"
    hardlink = data_dir / "hardlink.txt"
    independent = data_dir / "independent.txt"

    content = "UltraCopy combined link test\n"

    original.write_text(content, encoding="utf-8")

    # original.txt と同じinodeを共有する。
    os.link(original, hardlink)

    # 内容は同じだが、別inodeの独立ファイル。
    independent.write_text(content, encoding="utf-8")

    # ハードリンクされたファイルを参照するシンボリックリンク。
    link_original = links_dir / "link_original"
    link_hardlink = links_dir / "link_hardlink"

    os.symlink("../data/original.txt", link_original)
    os.symlink("../data/hardlink.txt", link_hardlink)

    # シンボリックリンクから別のシンボリックリンクを参照する。
    link_chain = links_dir / "link_chain"
    os.symlink("link_hardlink", link_chain)

    # コピー対象外の外部ファイルと相対シンボリックリンク。
    outside_file = outside / "outside.txt"
    outside_content = "External file must remain untouched\n"
    outside_file.write_text(outside_content, encoding="utf-8")

    external_link = links_dir / "external_relative"
    os.symlink("../../outside/outside.txt", external_link)

    print()
    print("===== コピー元の確認 =====")

    for path in (original, hardlink, independent):
        info = path.stat()
        print(
            f"{path.relative_to(source)}: "
            f"inode={info.st_ino}, nlink={info.st_nlink}"
        )

    print()
    print("===== コピー元リンク一覧 =====")

    for path in (
        link_original,
        link_hardlink,
        link_chain,
        external_link,
    ):
        print(f"{path.name}: {os.readlink(path)}")

    source_hardlinks_valid = (
        same_inode(original, hardlink)
        and original.stat().st_nlink == 2
    )

    source_independent_valid = not same_inode(
        original, independent
    )

    print()
    print("===== CopyWorker実行 =====")

    main.DATA_DIR = run_dir / "test_data"
    main.DB_PATH = main.DATA_DIR / "history.sqlite3"

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
        print(f"[ERROR] CopyWorker実行中に例外: {exc}")
        failed.append(str(exc))

    target = destination_parent / source.name

    copied_original = target / "data" / "original.txt"
    copied_hardlink = target / "data" / "hardlink.txt"
    copied_independent = target / "data" / "independent.txt"

    copied_link_original = target / "links" / "link_original"
    copied_link_hardlink = target / "links" / "link_hardlink"
    copied_link_chain = target / "links" / "link_chain"
    copied_external = target / "links" / "external_relative"

    print()
    print("===== コピー先の確認 =====")

    results = []

    results.append(check(
        source_hardlinks_valid,
        "コピー元の2ファイルが同じinodeを共有している"
    ))

    results.append(check(
        source_independent_valid,
        "コピー元の独立ファイルは別inodeである"
    ))

    results.append(check(
        bool(completed) and not failed,
        "CopyWorkerが正常終了した"
    ))

    required_files = [
        copied_original,
        copied_hardlink,
        copied_independent,
    ]

    files_exist = all(path.is_file() for path in required_files)

    results.append(check(
        files_exist,
        "コピー先に3つの通常ファイルが存在する"
    ))

    if files_exist:
        results.append(check(
            same_inode(copied_original, copied_hardlink),
            "コピー先でもハードリンク関係が維持された"
        ))

        results.append(check(
            not same_inode(copied_original, copied_independent),
            "独立ファイルが別inodeとして維持された"
        ))

        results.append(check(
            all(path.read_bytes() == content.encode("utf-8")
                for path in required_files),
            "コピー先3ファイルの内容が正しい"
        ))

        print()
        print("===== コピー先inode情報 =====")

        for path in required_files:
            info = path.stat()
            print(
                f"{path.relative_to(target)}: "
                f"inode={info.st_ino}, nlink={info.st_nlink}"
            )
    else:
        for description in (
            "コピー先でもハードリンク関係が維持された",
            "独立ファイルが別inodeとして維持された",
            "コピー先3ファイルの内容が正しい",
        ):
            results.append(check(False, description))

    copied_links = [
        copied_link_original,
        copied_link_hardlink,
        copied_link_chain,
        copied_external,
    ]

    links_exist = all(path.is_symlink() for path in copied_links)

    results.append(check(
        links_exist,
        "4つのシンボリックリンクがリンクとして複製された"
    ))

    if links_exist:
        results.append(check(
            os.readlink(copied_link_original)
            == "../data/original.txt"
            and os.readlink(copied_link_hardlink)
            == "../data/hardlink.txt"
            and os.readlink(copied_link_chain)
            == "link_hardlink"
            and os.readlink(copied_external)
            == "../../outside/outside.txt",
            "4つのシンボリックリンクのリンク先文字列が一致した"
        ))

        results.append(check(
            copied_link_original.read_text(encoding="utf-8")
            == content
            and copied_link_hardlink.read_text(encoding="utf-8")
            == content
            and copied_link_chain.read_text(encoding="utf-8")
            == content,
            "リンク連鎖を通じてコピー先データを読み取れる"
        ))

        results.append(check(
            same_inode(copied_link_original, copied_link_hardlink)
            and same_inode(copied_link_original, copied_original),
            "2つのシンボリックリンクが同じハードリンク実体を参照する"
        ))
    else:
        for description in (
            "4つのシンボリックリンクのリンク先文字列が一致した",
            "リンク連鎖を通じてコピー先データを読み取れる",
            "2つのシンボリックリンクが同じハードリンク実体を参照する",
        ):
            results.append(check(False, description))

    results.append(check(
        not copied_external.exists()
        and not (target / "outside").exists(),
        "外部リンク先の実体をコピー先へ複製していない"
    ))

    results.append(check(
        outside_file.read_text(encoding="utf-8")
        == outside_content,
        "コピー対象外の外部ファイルが変更されていない"
    ))

    results.append(check(
        same_inode(original, hardlink)
        and original.read_text(encoding="utf-8") == content
        and hardlink.read_text(encoding="utf-8") == content,
        "コピー元のハードリンク関係と内容が維持されている"
    ))

    print()
    print("=" * 70)
    print(
        f"試験結果: {sum(results)} / {len(results)} 項目 PASS"
    )
    print(f"保存場所: {run_dir}")
    print("=" * 70)

    if failed:
        print()
        print("===== エラー =====")
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
    sys.exit(main_test())
