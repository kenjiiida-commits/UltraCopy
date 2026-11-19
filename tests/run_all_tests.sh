#!/usr/bin/env bash
# UltraCopy の試験をすべて実行し、結果をまとめて表示する。
# 使い方（UltraCopy のフォルダーで）:  bash tests/run_all_tests.sh
# 先に ./起動.sh を一度実行して、仮想環境（.venv）を作っておくこと。
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "仮想環境がありません。先に ./起動.sh を一度実行してください。" >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    echo "注意: root で実行しています。権限に関する試験の一部は確認が不完全になります。"
    echo "      通常は一般ユーザーで実行してください。"
    echo
fi

# 画面のない環境でも動くようにする
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

total_pass=0
total_fail=0
failed_tests=()

for test in tests/test_*.py tests/symlink/test_*.py; do
    output=$("$PYTHON" "$test" 2>&1)
    pass=$(grep -c "\[PASS\]" <<<"$output")
    fail=$(grep -c "\[FAIL\]" <<<"$output")
    if [ "$fail" -gt 0 ] || grep -q "Traceback" <<<"$output"; then
        echo "✗ $test  (PASS $pass / FAIL $fail)"
        failed_tests+=("$test")
    elif [ "$pass" -eq 0 ]; then
        echo "- $test  （この環境では実行されませんでした）"
    else
        echo "✓ $test  (PASS $pass)"
    fi
    total_pass=$((total_pass + pass))
    total_fail=$((total_fail + fail))
done

echo
echo "合計: PASS $total_pass / FAIL $total_fail"
if [ ${#failed_tests[@]} -gt 0 ]; then
    echo "失敗した試験（詳しくは個別に実行してください）:"
    for test in "${failed_tests[@]}"; do
        echo "  $PYTHON $test"
    done
    exit 1
fi
