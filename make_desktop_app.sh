#!/bin/bash
# Makes "Odd Finder.app" on the Desktop: double-click it to start the bot and open the site. The bot
# runs in a Terminal window (macOS lets Terminal read the Desktop folder; apps started from Finder
# can't without extra permission). If the bot is already running it asks: Open, or Stop the bot.
# Run again after moving this folder:  ./make_desktop_app.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$HOME/Desktop}/Odd Finder.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Odd Finder</string>
  <key>CFBundleDisplayName</key><string>Odd Finder</string>
  <key>CFBundleIdentifier</key><string>local.oddfinder.launcher</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/launch" <<SCRIPT
#!/bin/bash
DIR="$DIR"
SCRIPT
cat >> "$APP/Contents/MacOS/launch" <<'SCRIPT'
URL="http://localhost:8765"
say() { osascript -e "display dialog \"$1\" buttons {\"OK\"} default button \"OK\" with title \"Odd Finder\"" >/dev/null 2>&1; }
up() { curl -s -o /dev/null --max-time 2 "$URL"; }

if up; then
  choice=$(osascript -e 'button returned of (display dialog "Odd Finder is already running." buttons {"Stop the bot", "Open"} default button "Open" with title "Odd Finder")' 2>/dev/null)
  if [ "$choice" = "Stop the bot" ]; then
    lsof -ti tcp:8765 -sTCP:LISTEN | xargs kill 2>/dev/null
    osascript -e 'display notification "The bot is stopped. Double-click Odd Finder to start it again." with title "Odd Finder"'
  elif [ "$choice" = "Open" ]; then
    open "$URL"
  fi
  exit 0
fi

if ! open -a Terminal "$DIR/start_bot.command" 2>/dev/null; then
  say "Can't find the bot in $DIR. If you moved the Value Finder folder, run make_desktop_app.sh in it again."
  exit 1
fi
for i in $(seq 1 120); do                 # the first start can take a minute or two (past results)
  if up; then open "$URL"; exit 0; fi
  sleep 1
done
say "The bot is taking a while to start. Check its Terminal window for a message."
SCRIPT
chmod +x "$APP/Contents/MacOS/launch"

# icon from the site's logo
if qlmanage -t -s 1024 -o "$TMP" "$DIR/static/logo.svg" >/dev/null 2>&1 && [ -f "$TMP/logo.svg.png" ]; then
  mkdir "$TMP/icon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/logo.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$TMP/logo.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/icon.icns" || true
fi
touch "$APP"
echo "Made $APP"
