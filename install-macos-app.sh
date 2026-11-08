#!/bin/sh
set -eu

if [ "$(uname -s)" != Darwin ]; then
  echo '这个启动器仅适用于 macOS。' >&2
  exit 1
fi

checkout=$(cd "${1:-$(dirname "$0")}" && pwd -P)
installer=$(cd "$(dirname "$0")" && pwd -P)
if [ ! -f "$checkout/launcher.py" ] || [ ! -x "$checkout/start.sh" ]; then
  echo '找不到 ScreenDesk 项目，请从项目目录运行这个安装脚本。' >&2
  exit 1
fi
if ! swiftc=$(/usr/bin/xcrun --find swiftc 2>/dev/null); then
  echo '请先运行 xcode-select --install 安装苹果命令行工具，再运行安装脚本。' >&2
  exit 1
fi

applications="$HOME/Applications"
destination="$applications/ScreenDesk.app"
if [ -L "$destination" ] || { [ -e "$destination" ] && {
  [ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$destination/Contents/Info.plist" 2>/dev/null || :)" != com.yankersey.screendesk ] ||
  [ "$(cat "$destination/Contents/Resources/checkout.txt" 2>/dev/null || :)" != "$checkout" ];
}; }; then
  echo "启动器已存在：$destination" >&2
  exit 1
fi

staging=$(mktemp -d)
install_staging=
trap 'rm -rf "$staging"; if [ -n "$install_staging" ]; then rm -rf "$install_staging"; fi' EXIT
bundle="$staging/ScreenDesk.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
printf '%s\n' "$checkout" > "$bundle/Contents/Resources/checkout.txt"
cp "$installer/assets/ScreenDesk.icns" "$bundle/Contents/Resources/ScreenDesk.icns"
"$swiftc" -sdk "$(/usr/bin/xcrun --sdk macosx --show-sdk-path)" \
  -swift-version 5 -parse-as-library -O -target "$(uname -m)-apple-macos12.0" \
  "$installer/macos/ScreenDesk.swift" -o "$bundle/Contents/MacOS/ScreenDesk"

cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key><string>ScreenDesk</string>
  <key>CFBundleName</key><string>ScreenDesk</string>
  <key>CFBundleIdentifier</key><string>com.yankersey.screendesk</string>
  <key>CFBundleExecutable</key><string>ScreenDesk</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>ScreenDesk.icns</string>
  <key>CFBundleVersion</key><string>3</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSDocumentsFolderUsageDescription</key><string>ScreenDesk 需要打开保存在「文稿」中的项目和工作区。</string>
</dict>
</plist>
PLIST

/usr/bin/plutil -lint "$bundle/Contents/Info.plist"
/usr/bin/codesign --sign - "$bundle"
mkdir -p "$applications"
install_staging=$(mktemp -d "$applications/.screendesk.XXXXXX")
/usr/bin/ditto "$bundle" "$install_staging/ScreenDesk.app"
/usr/bin/codesign --verify --strict "$install_staging/ScreenDesk.app"
if [ -e "$destination" ]; then
  mv "$destination" "$install_staging/previous.app"
fi
if ! mv "$install_staging/ScreenDesk.app" "$destination"; then
  if [ -e "$install_staging/previous.app" ]; then
    mv "$install_staging/previous.app" "$destination" || install_staging=
  fi
  exit 1
fi
printf '已安装 %s\n' "$destination"
