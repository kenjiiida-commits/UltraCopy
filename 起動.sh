#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# 動作に必要な Python のバージョン（3.10 以上）を確認する。
if ! command -v python3 >/dev/null 2>&1; then
    echo "エラー: python3 が見つかりません。" >&2
    echo "  sudo apt install python3 python3-venv  でインストールしてください。" >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "エラー: Python 3.10 以上が必要です（現在: $(python3 --version 2>&1)）。" >&2
    exit 1
fi

if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    echo "エラー: 仮想環境を作るための python3-venv がありません。" >&2
    echo "  sudo apt install python3-venv  でインストールしてください。" >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

if ! .venv/bin/python -c "import PySide6" >/dev/null 2>&1; then
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install PySide6
fi

exec .venv/bin/python src/main.py
