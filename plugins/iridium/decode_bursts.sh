#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# decode_bursts.sh — Stage 2b: watch iridium_bursts/ for new .cf32 files and
# decode each with iridium-toolkit (https://github.com/muccc/iridium-toolkit).
#
# Run this in a second terminal alongside SDRTerm.  The iridium plugin (with
# `c` capture toggled on) writes bursts as they're detected; this script picks
# them up, prints the sidecar metadata, invokes the external decoder, and
# moves processed files into iridium_bursts/done/.
#
# Portable polling: uses a plain `while true; sleep 1` loop so it works on
# macOS (no inotifywait) and Linux without any extra tools.
#
# ── prerequisites ─────────────────────────────────────────────────────────
#   iridium-extractor  (binary on PATH — provided by iridium-toolkit install)
#   iridium-parser.py  (Python script from iridium-toolkit repo)
#
# ── configuration ─────────────────────────────────────────────────────────
# Point IRIDIUM_PARSER at your iridium-parser.py.  If iridium-extractor is not
# on PATH, set IRIDIUM_EXTRACTOR too.  DETECT_DB matches the -d flag on the
# live pipeline (14 dB is the value the user has been running with).
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/iridium_bursts"
DONE="$DIR/done"
mkdir -p "$DIR" "$DONE"

IRIDIUM_EXTRACTOR="${IRIDIUM_EXTRACTOR:-iridium-extractor}"
IRIDIUM_PARSER="${IRIDIUM_PARSER:-/Users/martin/Projects/Hardware/sdr/iridium_decode/iridium-toolkit/iridium-parser.py}"
DETECT_DB="${DETECT_DB:-14}"

# ── helpers ────────────────────────────────────────────────────────────────
sidecar_field() {
    # $1 = sidecar json path, $2 = field name — plain-python read, no jq dep.
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"
}

decode_one() {
    local cf32="$1"
    local base="${cf32%.cf32}"
    local json="${base}.json"

    if [ ! -f "$json" ]; then
        echo "─── $(basename "$cf32") — no sidecar, skipping"
        mv "$cf32" "$DONE/" 2>/dev/null || true
        return
    fi

    local sr fc snr ch
    sr=$(sidecar_field "$json" sample_rate)
    fc=$(printf '%.0f' "$(sidecar_field "$json" tuned_center_hz)")
    snr=$(sidecar_field "$json" snr_db)
    ch=$(sidecar_field "$json" chan_id)

    echo "─── $(basename "$cf32")  ch=$ch  snr=${snr}dB  fc=${fc}  sr=${sr} ──"

    # iridium-extractor requires sample_rate / decimation to be a multiple of
    # 250 kHz.  Detect the incompatibility BEFORE invoking the pipeline —
    # otherwise the extractor errors and the 2>/dev/null below hides the
    # reason, leaving the user staring at silent output.
    if [ "$(( sr % 250000 ))" -ne 0 ]; then
        echo "  ⚠ sample_rate ${sr} is not a multiple of 250 000 Hz —"
        echo "    iridium-extractor will reject this file.  Run:"
        echo "      python3 $(dirname "$0")/downsample_bursts.py"
        echo "    to batch-convert existing captures to 2 MHz."
        # Don't move it to done/ — the user needs to convert then retry.
        return
    fi

    # Same pipeline as the live command, adapted to per-file input:
    #   live:  iridium-extractor -d 14 <soapy.conf> | iridium-parser.py --uw-ec --harder -
    #   file:  iridium-extractor -d 14 -f cf32_le -r SR -c FC <file> | iridium-parser.py …
    "$IRIDIUM_EXTRACTOR" -f cf32_le -r "$sr" -c "$fc" -d "$DETECT_DB" "$cf32" 2>/dev/null \
        | python3 -u "$IRIDIUM_PARSER" --uw-ec --harder - 2>/dev/null \
        || true

    mv "$cf32" "$json" "$DONE/" 2>/dev/null || true
}

# ── main loop ──────────────────────────────────────────────────────────────
echo "Watching $DIR for new .cf32 files (Ctrl+C to quit)..."
echo "Extractor: $IRIDIUM_EXTRACTOR   Parser: $IRIDIUM_PARSER   -d $DETECT_DB"
while true; do
    shopt -s nullglob
    files=("$DIR"/*.cf32)
    shopt -u nullglob

    if [ "${#files[@]}" -gt 0 ]; then
        for f in "${files[@]}"; do
            decode_one "$f"
        done
    fi
    sleep 1
done
