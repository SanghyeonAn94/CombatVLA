#!/bin/bash
# Build 5x2 grid video (2560x1440 = 16:9) of PASS-only TC recordings.
# Source: /mnt/e/CombatVLA/sample/tc_<NN>/tc_<NN>.mp4
# Layout: 10 layers (5 cols × 2 rows). Each layer is a sequential concat of TCs,
#         so when one TC ends in a cell, the next begins in the same cell.
# If TC count > 10, the script auto-distributes evenly per layer.

set -u
FFMPEG="/mnt/e/CombatVLA/.venv/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
SAMPLE_ROOT="/mnt/e/CombatVLA/sample"
OUT_DIR="/mnt/e/QA Records/_pass_grid"
FINAL="${OUT_DIR}/bvt_pass_grid_2560x1440.mp4"
FPS=30

# PASS TC numbers (43 total)
PASS_TCS=(4 5 6 7 13 14 15 16 17 20 22 24 27 28 29 30 32 33 34 36 37 39 40 42 44 46 47 48 49 50 52 53 54 55 56 57 58 59 64 65 66 67)

mkdir -p "$OUT_DIR"

# Collect existing mp4 files
FILES=()
for tc in "${PASS_TCS[@]}"; do
    f="${SAMPLE_ROOT}/tc_${tc}/tc_${tc}.mp4"
    if [ -f "$f" ]; then
        FILES+=("$f")
    else
        echo "[Warn] missing: $f"
    fi
done
N=${#FILES[@]}
echo "[Info] $N PASS recordings"
[ "$N" -eq 0 ] && { echo "No files."; exit 1; }

LAYERS=10
CHUNK=$(( (N + LAYERS - 1) / LAYERS ))
echo "[Info] $CHUNK TCs per cell (10 cells)"

# ---------- Stage 1: build each layer ----------
declare -a LAYER_PATHS=()
for ((layer=0; layer<LAYERS; layer++)); do
    start=$((layer * CHUNK))
    end=$((start + CHUNK))
    if [ $start -ge $N ]; then break; fi
    if [ $end -gt $N ]; then end=$N; fi

    list_file="${OUT_DIR}/layer_${layer}_list.txt"
    layer_out="${OUT_DIR}/layer_${layer}.mp4"
    LAYER_PATHS+=("$layer_out")
    : > "$list_file"

    echo ""
    echo "[Layer $layer] cells [$start..$((end-1))]:"
    for ((i=start; i<end; i++)); do
        src="${FILES[$i]}"
        echo "  - $(basename $(dirname "$src"))"
        norm="${OUT_DIR}/layer_${layer}_part_${i}.mp4"
        # 540x1170 portrait → fit into 512x720 cell (force 16:9-ish cell, letterbox black)
        "$FFMPEG" -y -i "$src" \
            -vf "scale=-2:720:force_original_aspect_ratio=decrease,pad=512:720:(ow-iw)/2:(oh-ih)/2:black,fps=$FPS,format=yuv420p" \
            -c:v libx264 -preset veryfast -crf 23 -an "$norm" 2>&1 | tail -1
        echo "file '${norm}'" >> "$list_file"
    done

    echo "[Layer $layer] Concat → $(basename "$layer_out")"
    "$FFMPEG" -y -f concat -safe 0 -i "$list_file" -c copy "$layer_out" 2>&1 | tail -1
done

# ---------- Stage 2: pad to longest + xstack ----------
LONGEST=0
for p in "${LAYER_PATHS[@]}"; do
    dur_line=$("$FFMPEG" -i "$p" 2>&1 | grep "Duration:" | head -1)
    dur=$(echo "$dur_line" | sed 's/.*Duration: \([0-9:.]*\).*/\1/')
    secs=$(echo "$dur" | awk -F: '{print ($1*3600)+($2*60)+$3}')
    intsec=${secs%.*}
    [ -z "$intsec" ] && intsec=0
    [ "$intsec" -gt "$LONGEST" ] && LONGEST=$intsec
done
echo ""
echo "[Info] Longest layer: ${LONGEST}s"

FFMPEG_ARGS=(-y)
FILTER=""
for ((i=0; i<${#LAYER_PATHS[@]}; i++)); do
    FFMPEG_ARGS+=(-i "${LAYER_PATHS[$i]}")
    FILTER+="[${i}:v]tpad=stop_mode=clone:stop_duration=${LONGEST}[v${i}];"
done
for ((i=${#LAYER_PATHS[@]}; i<10; i++)); do
    FILTER+="color=c=black:s=512x720:r=${FPS}:d=${LONGEST}[v${i}];"
done

FILTER+="[v0][v1][v2][v3][v4]hstack=inputs=5[top];"
FILTER+="[v5][v6][v7][v8][v9]hstack=inputs=5[bot];"
FILTER+="[top][bot]vstack=inputs=2[out]"

echo "[Final] Building → $FINAL"
"$FFMPEG" "${FFMPEG_ARGS[@]}" -filter_complex "$FILTER" \
    -map "[out]" -c:v libx264 -preset fast -crf 23 -t "$LONGEST" "$FINAL" 2>&1 | tail -5

echo ""
echo "=== Done ==="
ls -lh "$FINAL"
