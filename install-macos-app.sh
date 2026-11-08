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
trap 'rm -rf "$staging"' EXIT
bundle="$staging/ScreenDesk.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
printf '%s\n' "$checkout" > "$bundle/Contents/Resources/checkout.txt"
cp "$installer/assets/ScreenDesk.icns" "$bundle/Contents/Resources/ScreenDesk.icns"

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
  <key>CFBundleVersion</key><string>2</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$bundle/Contents/MacOS/ScreenDesk" <<'LAUNCHER'
#!/bin/sh
set -eu
contents=$(cd "$(dirname "$0")/.." && pwd -P)
exec /usr/bin/open -a Terminal "$contents/Resources/启动 ScreenDesk.command"
LAUNCHER

cat > "$bundle/Contents/Resources/启动 ScreenDesk.command" <<'COMMAND'
#!/bin/sh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
resources=$(cd "$(dirname "$0")" && pwd -P)
IFS= read -r checkout < "$resources/checkout.txt"
if [ ! -x "$checkout/start.sh" ]; then
  echo '找不到 ScreenDesk 项目。项目目录移动后，请重新安装启动器。' >&2
  exit 1
fi
exec "$checkout/启动 ScreenDesk.command"
COMMAND
chmod +x "$bundle/Contents/MacOS/ScreenDesk" "$bundle/Contents/Resources/启动 ScreenDesk.command"
/usr/bin/plutil -lint "$bundle/Contents/Info.plist"
/usr/bin/codesign --sign - "$bundle"
mkdir -p "$applications"
/usr/bin/ditto "$bundle" "$destination"
/usr/bin/codesign --verify --strict "$destination"
printf '已安装 %s\n' "$destination"
