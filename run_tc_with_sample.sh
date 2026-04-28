#!/bin/bash
# Run a single TC with recording + auto sample collection.
#
# Usage: ./run_tc_with_sample.sh <tc_no>
#
# Behavior:
#   1. Cleans captures + previous tc_<NN>.mp4 chunks
#   2. Starts chunked recorder (record_tc.sh) for tc_<NN>
#   3. Runs `python -m nikke_bvt.runner --test <tc_no>`
#   4. Stops recorder cleanly
#   5. Calls collect_tc_sample.py to bundle artifacts into sample/tc_<NN>/
#
# Continues regardless of PASS/FAIL — sample is collected either way.

set -u
TC_NO="${1:-}"
if [ -z "$TC_NO" ]; then
    echo "Usage: $0 <tc_no>"
    exit 1
fi

ROOT="/mnt/e/CombatVLA"
ADB="/mnt/c/Users/SHIFTUP/AppData/Local/Microsoft/WinGet/Packages/Genymobile.scrcpy_Microsoft.Winget.Source_8wekyb3d8bbwe/scrcpy-win64-v3.3.4/adb.exe"
QA="/mnt/e/QA Records"
LOG_DIR="$ROOT/bvt_logs/tc_samples_$(date +%Y%m%d)"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/tc_${TC_NO}.log"

echo "=== TC #$TC_NO ===" | tee -a "$LOG"

# Cleanup
rm -f "$ROOT/captures/"*.png 2>/dev/null
rm -f "$QA/tc_${TC_NO}_2026"*.mp4 "$QA/tc_${TC_NO}.mp4" 2>/dev/null

# Patch record_tc.sh prefix temporarily? No — record_tc.sh hardcodes tc_60_.
# Use a small wrapper that overrides PREFIX.
PREFIX_RECORDER="$ROOT/.tmp_recorder_${TC_NO}.sh"
cat > "$PREFIX_RECORDER" <<EOF
#!/bin/bash
ADB="$ADB"
SESSION_TS=\$(date +%Y%m%d_%H%M%S)
HOST_DIR="$QA"
DEVICE_DIR="/sdcard"
mkdir -p "\$HOST_DIR"
cleanup() {
    "\$ADB" shell pkill -SIGINT screenrecord 2>/dev/null
    sleep 3
    if [ -n "\$CURRENT_CHUNK" ]; then
        "\$ADB" pull "\$CURRENT_CHUNK" "E:\\\\QA Records\\\\" 2>/dev/null && "\$ADB" shell rm -f "\$CURRENT_CHUNK" 2>/dev/null
    fi
    exit 0
}
trap cleanup INT TERM
CHUNK=0
while true; do
    CHUNK_NAME="tc_${TC_NO}_\${SESSION_TS}_\$(printf '%03d' \$CHUNK).mp4"
    CURRENT_CHUNK="\$DEVICE_DIR/\$CHUNK_NAME"
    "\$ADB" shell screenrecord --time-limit 150 --bit-rate 2000000 --size 540x1170 "\$CURRENT_CHUNK"
    "\$ADB" pull "\$CURRENT_CHUNK" "E:\\\\QA Records\\\\" 2>/dev/null && "\$ADB" shell rm -f "\$CURRENT_CHUNK" 2>/dev/null
    CHUNK=\$((CHUNK + 1))
done
EOF
chmod +x "$PREFIX_RECORDER"

# Start recorder
bash "$PREFIX_RECORDER" > "$LOG_DIR/tc_${TC_NO}_recorder.log" 2>&1 &
RPID=$!
echo "[Recorder] PID=$RPID prefix=tc_${TC_NO}" | tee -a "$LOG"
sleep 4

# Run TC
echo "[TC] Starting #$TC_NO ..." | tee -a "$LOG"
source "$ROOT/.venv/bin/activate"
python -m nikke_bvt.runner --test "$TC_NO" 2>&1 | tee -a "$LOG"
TC_EXIT=$?

# Stop recorder
echo "[Recorder] Stopping..." | tee -a "$LOG"
"$ADB" shell pkill -SIGINT screenrecord 2>/dev/null
sleep 4
kill -TERM $RPID 2>/dev/null
sleep 2
kill -KILL $RPID 2>/dev/null
rm -f "$PREFIX_RECORDER"

# Collect sample (use orchestrator-merged tc_<NN>.mp4 if present, else chunks)
SINGLE="$QA/tc_${TC_NO}.mp4"
if [ -f "$SINGLE" ]; then
    REC_ARG="$SINGLE"
else
    REC_ARG=""
fi

echo "[Collector] Bundling sample..." | tee -a "$LOG"
if [ -n "$REC_ARG" ]; then
    python "$ROOT/collect_tc_sample.py" "$TC_NO" "$LOG" "$REC_ARG" 2>&1 | tee -a "$LOG"
else
    python "$ROOT/collect_tc_sample.py" "$TC_NO" "$LOG" 2>&1 | tee -a "$LOG"
fi

echo "[Done] TC #$TC_NO sample → $ROOT/sample/tc_$TC_NO" | tee -a "$LOG"
exit $TC_EXIT
