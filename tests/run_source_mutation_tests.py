from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TESTS = [
    (
        "コピー完了後のコピー元変更試験",
        PROJECT_ROOT / "tests/test_source_mutation_during_copy.py",
    ),
    (
        "コピー中のコピー元削除試験",
        PROJECT_ROOT / "tests/test_source_deletion_during_copy.py",
    ),
    (
        "データ転送途中のコピー元変更試験",
        PROJECT_ROOT / "tests/test_source_mutation_mid_copy.py",
    ),
    (
        "SHA-256計算直後のコピー元変更試験",
        PROJECT_ROOT / "tests/test_source_mutation_after_hash.py",
    ),
    (
        "ハードリンク経路のコピー元変更試験",
        PROJECT_ROOT / "tests/test_hardlink_source_mutation_after_hash.py",
    ),
]


def main():
    print("=" * 72)
    print("UltraCopy Ver.0.7 コピー元変更・削除・SHA-256再検証 回帰試験 一括実行")
    print("=" * 72, flush=True)

    results = []

    for title, script in TESTS:
        print()
        print("=" * 72)
        print(title)
        print("=" * 72, flush=True)

        if not script.is_file():
            print(f"[ERROR] 試験ファイルがありません: {script}")
            results.append((title, False))
            continue

        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=PROJECT_ROOT,
                check=False,
            )
            success = result.returncode == 0
        except OSError as exc:
            print(f"[ERROR] 試験を起動できません: {exc}")
            success = False

        results.append((title, success))
        print()
        print(f"{title}: {'PASS' if success else 'FAIL'}")

    print()
    print("=" * 72)
    print("一括試験 最終結果")
    print("=" * 72)

    for title, success in results:
        print(f"[{'PASS' if success else 'FAIL'}] {title}")

    passed = sum(success for _, success in results)
    total = len(results)

    print("-" * 72)
    print(f"総合結果: {passed} / {total} 試験 PASS")
    print("=" * 72)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
