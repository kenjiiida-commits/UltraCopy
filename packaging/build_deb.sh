#!/usr/bin/env bash
# UltraCopy の DEB パッケージを作る。
# 使い方（UltraCopy のフォルダーで）:  bash packaging/build_deb.sh
# 出来上がり: dist/ultracopy_<バージョン>_all.deb
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' src/main.py)
if [ -z "$VERSION" ]; then
    echo "src/main.py からバージョンを読み取れません。" >&2
    exit 1
fi
PACKAGE="ultracopy"
BUILD="build/deb/${PACKAGE}_${VERSION}_all"
OUTPUT="dist/${PACKAGE}_${VERSION}_all.deb"

rm -rf "$BUILD"
mkdir -p "$BUILD/DEBIAN" \
         "$BUILD/usr/bin" \
         "$BUILD/usr/share/$PACKAGE" \
         "$BUILD/usr/share/applications" \
         "$BUILD/usr/share/icons/hicolor/scalable/apps" \
         "$BUILD/usr/share/doc/$PACKAGE" \
         "$BUILD/usr/share/man/man1" \
         dist

# 本体
install -m 644 src/main.py "$BUILD/usr/share/$PACKAGE/main.py"

# 起動用コマンド（/usr/bin/ultracopy）
cat > "$BUILD/usr/bin/$PACKAGE" <<'LAUNCHER'
#!/bin/sh
# UltraCopy を起動する。Ubuntu の python3 と PySide6 パッケージを使う。
exec /usr/bin/python3 -I /usr/share/ultracopy/main.py "$@"
LAUNCHER
chmod 755 "$BUILD/usr/bin/$PACKAGE"

# アプリ一覧への登録とアイコン
install -m 644 packaging/ultracopy.desktop "$BUILD/usr/share/applications/ultracopy.desktop"
install -m 644 packaging/ultracopy.svg "$BUILD/usr/share/icons/hicolor/scalable/apps/ultracopy.svg"

# 説明書（man ultracopy）
gzip -9n < packaging/ultracopy.1 > "$BUILD/usr/share/man/man1/ultracopy.1.gz"
chmod 644 "$BUILD/usr/share/man/man1/ultracopy.1.gz"

# 著作権表示と変更履歴
{
    echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    echo "Upstream-Name: UltraCopy"
    echo "Source: https://github.com/kenjiiida-commits/UltraCopy"
    echo
    echo "Files: *"
    echo "Copyright: 2026 飯田顕治"
    echo "License: MIT"
    tail -n +5 LICENSE | sed 's/^/ /; s/^ $/ ./'
} > "$BUILD/usr/share/doc/$PACKAGE/copyright"
chmod 644 "$BUILD/usr/share/doc/$PACKAGE/copyright"
{
    echo "$PACKAGE ($VERSION) unstable; urgency=medium"
    echo
    echo "  * UltraCopy $VERSION。詳しい変更点は README の変更履歴を参照。"
    echo
    echo " -- 飯田顕治 <kenjiiida-commits@users.noreply.github.com>  $(date -R)"
} | gzip -9n > "$BUILD/usr/share/doc/$PACKAGE/changelog.gz"
chmod 644 "$BUILD/usr/share/doc/$PACKAGE/changelog.gz"

# パッケージ情報
INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "$BUILD" | cut -f1)
cat > "$BUILD/DEBIAN/control" <<CONTROL
Package: $PACKAGE
Version: $VERSION
Architecture: all
Maintainer: 飯田顕治 <kenjiiida-commits@users.noreply.github.com>
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.10), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets
Section: utils
Priority: optional
Homepage: https://github.com/kenjiiida-commits/UltraCopy
Description: Safe GUI file copier with SHA-256 verification
 UltraCopy copies files and folders into a hidden staging area on the
 destination file system, verifies them with SHA-256 by re-reading the
 destination from disk, commits the data with fsync/syncfs and then
 publishes the result without ever overwriting existing items.
 .
 SHA-256 で検証し、既存の項目を上書きしない、安全重視のファイルコピーソフト。
CONTROL

# 所有者を root にして作る（作った人のユーザー名が中に残らないように）
dpkg-deb --root-owner-group -Zxz --build "$BUILD" "$OUTPUT" >/dev/null
echo "作成しました: $OUTPUT"
dpkg-deb --info "$OUTPUT" | sed -n 's/^ *\(Package\|Version\|Depends\|Installed-Size\):/  &/p'
