#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# decode_bursts.sh — Stage 2b: batch-decode captured Iridium bursts.
#
# Watches iridium_bursts/, groups .cf32 files by (tuned_center_hz, sample_rate),
# concatenates each same-slot batch into one continuous stream, and pipes that
# stream through iridium-toolkit's extractor + parser.
#
# Why batch?  iridium-extractor's fft-burst-tagger uses a running noise-floor
# EMA that needs a couple of seconds of runway to converge.  Individual
# ~100 ms per-burst files are too short — the tagger never fires on them.
# Concatenating BATCH_MIN files at the same freqhop slot gives the tagger
# enough context to detect and demod the bursts.
#
# Concatenation of raw complex64_le files is safe with `cat` — the files
# contain nothing but interleaved float32 I/Q pairs; joining them end-to-end
# just yields a longer IQ stream at the same sample rate.
#
# ── prerequisites ─────────────────────────────────────────────────────────
#   iridium-extractor    (binary on PATH — from iridium-toolkit)
#   iridium-parser.py    (Python script from iridium-toolkit)
#   Python: crcmod       — install in the parser's interpreter:
#       /opt/homebrew/bin/python3 -m pip install --break-system-packages crcmod
#     (or `pipx inject iridium-toolkit crcmod` if installed via pipx)
#
# ── env-var overrides ─────────────────────────────────────────────────────
#   IRIDIUM_EXTRACTOR    default: "iridium-extractor"
#   IRIDIUM_PARSER       default: hard-coded to a local path — edit for yours
#   DETECT_DB            extractor -d threshold (default 14; try 8 on weak)
#   BATCH_MIN            files per stitched batch (default 20 ≈ 2 s at 100 ms)
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/iridium_bursts"
DONE="$DIR/done"
TMP="$DIR/.stitch"
mkdir -p "$DIR" "$DONE" "$TMP"

IRIDIUM_EXTRACTOR="${IRIDIUM_EXTRACTOR:-iridium-extractor}"
IRIDIUM_PARSER="${IRIDIUM_PARSER:-/Users/martin/Projects/Hardware/sdr/iridium_decode/iridium-toolkit/iridium-parser.py}"
# Pin the parser's Python interpreter — DO NOT let PATH pick it, because
# users often run this from a virtualenv shell where `python3` is the venv
# python and crcmod isn't installed there.  crcmod belongs to whichever
# python was used to install iridium-toolkit — typically Homebrew's.
IRIDIUM_PARSER_PY="${IRIDIUM_PARSER_PY:-/opt/homebrew/bin/python3}"
DETECT_DB="${DETECT_DB:-14}"
BATCH_MIN="${BATCH_MIN:-20}"

# ── preflight: confirm crcmod is importable in the parser's interpreter ──
if ! "$IRIDIUM_PARSER_PY" -c 'import crcmod' 2>/dev/null; then
    echo "ERROR: crcmod is not importable in $IRIDIUM_PARSER_PY" >&2
    echo "       iridium-parser.py needs it to decode frames." >&2
    echo "       Install with:" >&2
    echo "         $IRIDIUM_PARSER_PY -m pip install --break-system-packages crcmod" >&2
    echo "       Or set IRIDIUM_PARSER_PY to a python that has crcmod installed." >&2
    exit 1
fi
if [ ! -f "$IRIDIUM_PARSER" ]; then
    echo "ERROR: iridium-parser.py not found at $IRIDIUM_PARSER" >&2
    echo "       Set IRIDIUM_PARSER to its actual path." >&2
    exit 1
fi
if ! command -v "$IRIDIUM_EXTRACTOR" >/dev/null 2>&1; then
    echo "ERROR: iridium-extractor not found (checked: $IRIDIUM_EXTRACTOR)" >&2
    echo "       Install iridium-toolkit or set IRIDIUM_EXTRACTOR." >&2
    exit 1
fi

# Decode one batch of same-slot files.  All must share (fc, sr).
process_batch() {
    local fc="$1"; shift
    local sr="$1"; shift
    # Remaining args are file paths.

    if [ "$(( sr % 250000 ))" -ne 0 ]; then
        echo "  ⚠ sample_rate ${sr} is not a multiple of 250 000 Hz —"
        echo "    run: python3 $(dirname "$0")/downsample_bursts.py"
        return
    fi

    local n="$#"
    local stitched="$TMP/batch_${fc}_$$_$(date +%s).cf32"

    echo "─── batch fc=${fc} Hz  sr=${sr} Hz  files=${n} ──"
    cat "$@" > "$stitched"

    "$IRIDIUM_EXTRACTOR" -f cf32_le -r "$sr" -c "$fc" -d "$DETECT_DB" "$stitched" 2>/dev/null \
        | "$IRIDIUM_PARSER_PY" -u "$IRIDIUM_PARSER" --uw-ec --harder - 2>/dev/null \
        || true

    rm -f "$stitched"

    # Move all inputs (cf32 + json) to done/
    for f in "$@"; do
        mv "$f" "${f%.cf32}.json" "$DONE/" 2>/dev/null || true
    done
}

# Emit one line per (fc, sr) group: "fc<TAB>sr<TAB>file1<TAB>file2<TAB>..."
# One python call, no bash quoting drama around array-into-string embedding.
group_files_by_center() {
    DIR="$DIR" python3 <<'PY'
import json, os, sys
from collections import defaultdict
d = os.environ['DIR']
groups = defaultdict(list)
for name in sorted(os.listdir(d)):
    if not name.endswith('.cf32'):
        continue
    cf32 = os.path.join(d, name)
    js   = cf32[:-5] + '.json'
    if not os.path.isfile(js):
        continue
    try:
        m = json.load(open(js))
    except Exception:
        continue
    key = (int(m['tuned_center_hz']), int(m['sample_rate']))
    groups[key].append(cf32)
for (fc, sr), files in groups.items():
    sys.stdout.write('{}\t{}\t{}\n'.format(fc, sr, '\t'.join(files)))
PY
}

echo "Watching $DIR for .cf32 files (Ctrl+C to quit)..."
echo "Extractor:  $IRIDIUM_EXTRACTOR"
echo "Parser:     $IRIDIUM_PARSER"
echo "Parser py:  $IRIDIUM_PARSER_PY"
echo "Config:     -d $DETECT_DB   batch_min=$BATCH_MIN   done→$DONE"

while true; do
    while IFS=$'\t' read -r fc sr rest; do
        [ -z "$fc" ] && continue
        IFS=$'\t' read -ra files <<< "$rest"
        if [ "${#files[@]}" -lt "$BATCH_MIN" ]; then
            continue
        fi
        # Process in fixed-size slices of BATCH_MIN files
        for ((i=0; i+BATCH_MIN<=${#files[@]}; i+=BATCH_MIN)); do
            process_batch "$fc" "$sr" "${files[@]:i:BATCH_MIN}"
        done
    done < <(group_files_by_center)
    sleep 2
done
