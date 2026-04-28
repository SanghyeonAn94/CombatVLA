#!/bin/bash
# Run a sequence of TCs with sample collection. Continues regardless of PASS/FAIL.
# Usage: ./run_tc_batch.sh <tc_no_1> <tc_no_2> ...

set -u
ROOT="/mnt/e/CombatVLA"
SUMMARY="$ROOT/sample/_batch_summary.log"

for TC_NO in "$@"; do
    echo "===== TC #$TC_NO start $(date +%H:%M:%S) =====" | tee -a "$SUMMARY"
    bash "$ROOT/run_tc_with_sample.sh" "$TC_NO" > /tmp/tc_${TC_NO}_out.log 2>&1
    EXIT=$?
    LAST=$(grep -E "Final|TC #${TC_NO}\] (PASS|FAIL)" /mnt/e/CombatVLA/bvt_logs/tc_samples_*/tc_${TC_NO}.log 2>/dev/null | tail -2 | tr '\n' ' | ')
    echo "TC #$TC_NO exit=$EXIT $LAST" | tee -a "$SUMMARY"
    sleep 3
done

echo "===== Batch DONE $(date +%H:%M:%S) =====" | tee -a "$SUMMARY"
