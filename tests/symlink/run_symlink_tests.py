from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TESTS = [
    ("基本シンボリックリンク試験",
     PROJECT_ROOT / "tests/symlink/test_basic_symlinks.py"),

    ("高度なシンボリックリンク試験",
     PROJECT_ROOT / "tests/symlink/test_advanced_symlinks.py"),

    ("シンボリックリンク属性自動試験",
     PROJECT_ROOT / "tests/symlink/test_symlink_attributes.py"),
]


def main():
    print("=" * 70)
    print("UltraCopy Ver.0.7 シンボリックリンク自動試験 一括実行")
    print("=" * 70)

    results = []

    for title, script in TESTS:
        print()
        print("=" * 70)
        print(title)
        print("=" * 70)

        if not script.is_file():
            print(f"[ERROR] 試験ファイルがありません: {script}")
            results.append((title, False))
            continue

        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=PROJECT_ROOT
        )

        success = result.returncode == 0
        results.append((title, success))

        print()
        print(f"{title}: {'PASS' if success else 'FAIL'}")

    print()
    print("=" * 70)
    print("一括試験 最終結果")
    print("=" * 70)

    for title, success in results:
        print(f"[{'PASS' if success else 'FAIL'}] {title}")

    passed = sum(success for _, success in results)

    print("-" * 70)
    print(f"総合結果: {passed} / {len(results)} 試験 PASS")
    print("=" * 70)

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
