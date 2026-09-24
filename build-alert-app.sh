#!/bin/bash
# Builds CoinLaunchAlert.app: a no-UI AppleScript applet that posts one banner.
# Plain `osascript` notifications are filed under Script Editor, which cannot be
# enabled on this Mac; an applet registers as its own notification client.
# The agent writes title/body to data/alert.txt, then runs `open -g` on the app.
set -euo pipefail
cd "$(dirname "$0")"
APP=CoinLaunchAlert.app
rm -rf "$APP"
osacompile -o "$APP" -e '
set f to (POSIX path of (path to home folder)) & "coin-launch-agent/data/alert.txt"
set L to paragraphs of (do shell script "cat " & quoted form of f)
display notification (item 2 of L) with title (item 1 of L)
delay 2'
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.dhruv.coinlaunch.alert" "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.dhruv.coinlaunch.alert" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP"
echo "built $APP"
