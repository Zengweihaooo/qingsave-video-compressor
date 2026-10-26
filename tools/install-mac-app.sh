#!/bin/bash
# Create ~/Applications/轻存.app that starts this checkout and opens it in the browser.
# The original Dock app wraps the page in its own window; this one is a plain launcher
# for new machines. Usage: bash tools/install-mac-app.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/轻存.app"
if [ -d "$APP" ] && [ -x "$APP/Contents/MacOS/QingSave" ] && file "$APP/Contents/MacOS/QingSave" | grep -q Mach-O; then
  echo "已存在原版 $APP（带自己的窗口），不覆盖。它会读取 Info.plist 里的 QingSaveRoot：" >&2
  /usr/libexec/PlistBuddy -c 'Print :QingSaveRoot' "$APP/Contents/Info.plist" >&2 || true
  exit 0
fi
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/MacOS/QingSave" <<EOF
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$ROOT" || exit 1
if [ ! -x .venv/bin/python3 ]; then python3 -m venv .venv || exit 1; fi
.venv/bin/python3 -m pip show Send2Trash >/dev/null 2>&1 || .venv/bin/python3 -m pip install -r requirements.txt
exec .venv/bin/python3 web_app.py
EOF
chmod +x "$APP/Contents/MacOS/QingSave"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>轻存</string>
<key>CFBundleDisplayName</key><string>轻存</string>
<key>CFBundleExecutable</key><string>QingSave</string>
<key>CFBundleIdentifier</key><string>com.zengweihao.qingsave.launcher</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>QingSaveRoot</key><string>$ROOT</string>
</dict></plist>
EOF
echo "已创建 $APP ，可以拖到 Dock。"
