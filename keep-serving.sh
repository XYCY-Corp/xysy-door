#!/bin/bash
# Keep Hermes' server — and therefore the XYCY door — running across reboots, on macOS.
#
# Run it once:   bash ~/.hermes/plugins/xycy/keep-serving.sh
#
# It writes a LaunchAgent, starts it, and then asks the door whether it is answering. It removes
# nothing, and running it twice is safe. To undo:
#   launchctl bootout gui/$(id -u)/ai.xycy.hermes-serve
#   rm ~/Library/LaunchAgents/ai.xycy.hermes-serve.plist
set -u

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="$HERMES_HOME/hermes-agent/venv/bin/python"
HZ="$HERMES_HOME/hermes-agent/hermes"
PORT="${XYCY_DOOR_PORT:-4850}"
LABEL="ai.xycy.hermes-serve"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo ""
echo "  Keeping Hermes' server up"
echo "  ========================="

if [ ! -x "$PY" ] || [ ! -f "$HZ" ]; then
  echo "  ! Could not find Hermes' own python at:"
  echo "      $PY"
  echo "    Is HERMES_HOME right? Nothing was changed."
  exit 1
fi
echo "  python : $PY"

mkdir -p "$HOME/Library/LaunchAgents" "$HERMES_HOME/logs" || exit 1

# launchd does not expand ~, so every path here is written out in full.
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$HZ</string>
    <string>serve</string>
    <string>--skip-build</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
    <key>HERMES_HOME</key><string>$HERMES_HOME</string>
  </dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$HERMES_HOME/logs/serve-launchd.log</string>
  <key>StandardErrorPath</key><string>$HERMES_HOME/logs/serve-launchd.log</string>
</dict></plist>
PLIST_EOF

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST" >/dev/null 2>&1 || { echo "  ! The generated file is not valid. Nothing started."; exit 1; }
fi
echo "  wrote  : $PLIST"

# A server already running by hand would hold the port and make launchd's copy fail on start.
"$PY" "$HZ" serve --stop >/dev/null 2>&1
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null
echo "  started, and set to run at login"

echo ""
echo "  Waiting for the door..."
ANS=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 3
  ANS="$(curl -s -m 4 "http://127.0.0.1:$PORT/xycy/hello" 2>/dev/null)"
  [ -n "$ANS" ] && break
done

echo ""
if [ -n "$ANS" ]; then
  echo "  OK  $ANS"
  echo ""
  echo "  The door is up and will come back after a reboot."
  echo "  Open XYCY at:  http://127.0.0.1:$PORT/xycy"
else
  echo "  ?   Nothing answered on 127.0.0.1:$PORT after 30 seconds."
  echo "      Last lines of the log:"
  tail -12 "$HERMES_HOME/logs/serve-launchd.log" 2>/dev/null | sed 's/^/        /'
fi
echo ""
