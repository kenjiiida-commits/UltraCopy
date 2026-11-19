from pathlib import Path
import sys
import tempfile
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import main


def main_test():
    print("=" * 76)
    print("UltraCopy Ver.0.7 renameat2実行直前のコピー先親ディレクトリ置換試験")
    print("=" * 76)

    runs_root = PROJECT_ROOT / "tests" / "file_attributes" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_dir = Path(tempfile.mkdtemp(
        prefix="destination_parent_at_syscall_",
        dir=runs_root
    ))
    print(f"試験作業場所: {run_dir}")

    test_data = run_dir / "test_data"
    test_data.mkdir()

    main.DATA_DIR = test_data
    main.DB_PATH = test_data / "history.sqlite3"

    source = run_dir / "source_data"
    destination_parent = run_dir / "destination"
    displaced_parent = run_dir / "destination_displaced"

    source.mkdir()
    destination_parent.mkdir()

    source_file = source / "test.txt"
    source_content = b"ULTRACOPY DESTINATION RACE TEST\n" * 1024
    source_file.write_bytes(source_content)

    target = destination_parent / source.name
    displaced_target = displaced_parent / source.name

    worker = main.CopyWorker(
        source,
        destination_parent,
        verify=True
    )

    completed_events = []
    failed_events = []
    logs = []

    worker.completed.connect(completed_events.append)
    worker.failed.connect(failed_events.append)
    worker.log.connect(logs.append)

    injection = {
        "executed": False,
        "parent_moved": False,
        "replacement_created": False,
    }

    original_cdll = main.ctypes.CDLL

    class Renameat2Interceptor:
        def __init__(self, real_libc):
            object.__setattr__(self, "_real_libc", real_libc)

        def __getattr__(self, name):
            if name == "renameat2":
                real_function = getattr(
                    self._real_libc,
                    "renameat2"
                )

                def intercepted_renameat2(*args):
                    if not injection["executed"]:
                        print(
                            "\n[試験操作] renameat2実行直前に"
                            "コピー先親ディレクトリを移動します。"
                        )

                        destination_parent.rename(displaced_parent)

                        injection["parent_moved"] = (
                            displaced_parent.is_dir()
                            and not destination_parent.exists()
                        )

                        destination_parent.mkdir()

                        injection["replacement_created"] = (
                            destination_parent.is_dir()
                        )

                        injection["executed"] = True

                        print(
                            "[試験操作] 元のパスに新しい親ディレクトリを"
                            "作成しました。"
                        )

                    return real_function(*args)

                return intercepted_renameat2

            return getattr(self._real_libc, name)

        def __setattr__(self, name, value):
            setattr(self._real_libc, name, value)

    def intercepted_cdll(*args, **kwargs):
        real_libc = original_cdll(*args, **kwargs)
        return Renameat2Interceptor(real_libc)

    main.ctypes.CDLL = intercepted_cdll

    print("\n===== CopyWorker実行 =====")

    try:
        worker.run()
    except Exception:
        print("[予期しない例外]")
        traceback.print_exc()
        return 1
    finally:
        main.ctypes.CDLL = original_cdll

    checks = []

    def check(label, condition):
        result = bool(condition)
        checks.append(result)
        print(f"[{'PASS' if result else 'FAIL'}] {label}")

    print("\n===== 置換タイミングの検証 =====")

    check(
        "renameat2実行直前の置換処理が実行された",
        injection["executed"]
    )

    check(
        "元の親ディレクトリが別名に移動された",
        injection["parent_moved"]
    )

    check(
        "元のパスに新しい親ディレクトリが作成された",
        injection["replacement_created"]
    )

    print("\n===== 公開先の検証 =====")

    check(
        "現在のコピー先パスに新しい親ディレクトリが存在する",
        destination_parent.is_dir()
    )

    check(
        "現在のコピー先パスには完成品が存在しない",
        not target.exists()
    )

    check(
        "移動先の古い親ディレクトリに完成品が公開されている",
        displaced_target.is_dir()
    )

    check(
        "移動先の完成品ファイルが存在する",
        (displaced_target / source_file.name).is_file()
    )

    check(
        "移動先の完成品ファイルの内容が正しい",
        (displaced_target / source_file.name).is_file()
        and (
            displaced_target / source_file.name
        ).read_bytes() == source_content
    )

    print("\n===== 通知・清掃の検証 =====")

    check(
        "成功通知が発生していない",
        len(completed_events) == 0
    )

    check(
        "失敗通知が1回発生した",
        len(failed_events) == 1
    )

    check(
        "失敗通知に親ディレクトリ置換の説明が含まれる",
        len(failed_events) == 1
        and "コピー先ディレクトリが公開処理中に置き換えられました"
        in failed_events[0]
    )

    check(
        "新しいコピー先親ディレクトリに余計な項目がない",
        destination_parent.is_dir()
        and not any(destination_parent.iterdir())
    )

    remaining_staging = list(
        displaced_parent.glob(".ultracopy-staging-*")
    ) if displaced_parent.exists() else []

    check(
        "移動先にステージング領域が残っていない",
        len(remaining_staging) == 0
    )

    partial_files = list(run_dir.rglob("*.partial"))

    check(
        "不完全な一時ファイルが残っていない",
        len(partial_files) == 0
    )

    print("\n===== 通知内容 =====")
    for event in completed_events:
        print(f"成功通知: {event}")

    for event in failed_events:
        print(f"失敗通知: {event}")

    print("\n===== 関連ログ =====")
    for line in logs:
        if any(word in line for word in (
            "コピー開始",
            "公開",
            "完了",
            "エラー",
            "清掃",
        )):
            print(line)

    passed = sum(checks)
    total = len(checks)

    print("\n" + "=" * 76)
    print(f"試験結果: {passed} / {total} 項目 PASS")
    print(f"保存場所: {run_dir}")
    print("=" * 76)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main_test())
