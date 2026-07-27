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
#   iridium-toolkit (Python 3):
#       git clone https://github.com/muccc/iridium-toolkit
#       cd iridium-toolkit
#       pip install .
#   → provides iridium-extractor + iridium-parser.py in PATH
#
#   Adjust IRIDIUM_EXTRACTOR / IRIDIUM_PARSER below if the binaries live
#   elsewhere on your machine.
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)/iridium_bursts"
DONE="$DIR/done"
mkdir -p "$DIR" "$DONE"

# ── external tools (edit paths if not on PATH) ─────────────────────────────
IRIDIUM_EXTRACTOR="${IRIDIUM_EXTRACTOR:-iridium-extractor}"
IRIDIUM_PARSER="${IRIDIUM_PARSER:-iridium-parser.py}"

# Configuration template for iridium-extractor — some builds want a config
# file rather than CLI flags.  Point EXTRACTOR_CONF at your local .conf if so.
EXTRACTOR_CONF="${EXTRACTOR_CONF:-}"

# ── helpers ────────────────────────────────────────────────────────────────
print_sidecar() {
    local json="$1"
    [ -f "$json" ] || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -c '{ts: .timestamp, ch: .chan_id, freq: .chan_freq_hz,
                snr: .snr_db, samples: .n_samples}' "$json"
    else
        cat "$json"
    fi
}

decode_one() {
    local cf32="$1"
    local base="${cf32%.cf32}"
    local json="${base}.json"

    echo "─── $(basename "$cf32") ──────────────────────────────"
    print_sidecar "$json"

    # ── invoke iridium-toolkit here ──────────────────────────────────────
    # Choose ONE of these depending on your iridium-toolkit build:
    #
    #   (a) iridium-extractor consumes wide IQ, outputs bit files piped to parser
    #       "$IRIDIUM_EXTRACTOR" ${EXTRACTOR_CONF:+-c "$EXTRACTOR_CONF"} \
    #           --file "$cf32" | "$IRIDIUM_PARSER"
    #
    #   (b) Some builds expect a raw-IQ config; check `iridium-extractor --help`
    #
    # For now this is a template — uncomment / adjust for your local install:
    echo "  (decoder invocation not yet configured — edit decode_bursts.sh)"

    mv "$cf32" "$json" "$DONE/" 2>/dev/null || true
}

# ── main loop ──────────────────────────────────────────────────────────────
echo "Watching $DIR for new .cf32 files (Ctrl+C to quit)..."
while true; do
    # nullglob-equivalent: skip if no files match
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
