#!/bin/zsh
# swift build → 手装 .app → ad-hoc 签名。产物放在不被 Spotlight 索引的 build.noindex。
set -euo pipefail
cd "${0:A:h}/.."
swift build -c release
BIN="$(swift build -c release --show-bin-path)/BoxzOffice"
APP="build.noindex/BOXZ OFFICE.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/BoxzOffice"
cp Resources/Info.plist "$APP/Contents/Info.plist"
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
mkdir -p "$APP/Contents/Resources/Fonts"
cp Resources/Fonts/InstrumentSans-Variable.ttf Resources/Fonts/OFL.txt "$APP/Contents/Resources/Fonts/"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP"          # ad-hoc：本机双击可开；分发要 Developer ID 与公证
echo "built: $APP"
