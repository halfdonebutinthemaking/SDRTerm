"""Group unparsed RAW: bursts by common bit-patterns to identify
candidate new message types for the parser.

Reads a log file produced by the plugin's `s` shortcut (or piped
gr-iridium output), runs each line through the vendored parser to
sort classified from unclassified, then buckets the RAW ones by
several heuristics and reports what looks like recurring structure.

Usage:
    uv run python -m plugins.iridium_decoder.analyze_raw <path/to/*.raw>

    # Or feed multiple files:
    uv run python -m plugins.iridium_decoder.analyze_raw iridium_logs/*.raw

Report sections:
  1. Classification breakdown — how many bursts became IRI/VOC/…/RAW
  2. RAW frames by DL/UL — direction split
  3. RAW frames by first 24 bits after UW (candidate LCW type-code)
     — recurring codes are potential new message classes
  4. RAW frames by symbol-length bucket — some Iridium types are
     fixed-length, so a spike at one length is a strong signal
  5. RAW frames by frequency band — 1616-1626.5 (duplex) vs
     1626.0-1626.5 (simplex) different message families
  6. Top-N candidate patterns — first 32 bits after UW, sorted by
     frequency of occurrence, with sample bursts for hand-inspection
"""
import argparse
import glob
import os
import re
import sys
from collections import Counter, defaultdict


def _import_parser():
    """Import the vendored parser exactly as the plugin does."""
    from . import parser as _pkg   # runs its side-effect sys.path insert
    from . import parser
    return parser.parse_line


_RAW_RE = re.compile(
    r'^RAW:\s+\S+\s+(?P<ts>\d+\.\d+)\s+(?P<freq>\d+)\s+'
    r'N:(?P<mag>[-+]?\d+\.\d+)(?P<noise>[-+]\d+\.\d+)\s+'
    r'I:(?P<id>\d+)\s+(?P<conf>\d+)%\s+(?P<level>\S+)\s+'
    r'(?P<nsyms>\d+)\s+(?P<bits>[01]+)\s*$'
)

_DL_UW = '001100000011000011110011'
_UL_UW = '110011000011110011111100'

# ── Iridium channel plan (for frequency-alignment filter) ──────────────
# Iridium L-band uses 41.667 kHz channel spacing starting at 1616.0 MHz,
# with a duplex block below 1626 MHz and a simplex block above.  Real
# bursts land within ±5 kHz of a channel centre; anything further is
# almost certainly a spur / interferer that accidentally passed our
# UW check via bit-error correction.
_IRIDIUM_BAND_LOW_HZ  = 1_616_000_000
_IRIDIUM_CHAN_SPACING = 25_000_000.0 / 600.0    # ≈ 41 666.667 Hz
_MAX_CHAN_DEVIATION   =  5_000                  # ± 5 kHz


def _channel_deviation(freq_hz: int) -> float:
    """Distance from `freq_hz` to the nearest Iridium channel centre, in Hz."""
    rel = (freq_hz - _IRIDIUM_BAND_LOW_HZ) / _IRIDIUM_CHAN_SPACING - 0.5
    nearest = round(rel)
    dev = (rel - nearest) * _IRIDIUM_CHAN_SPACING
    return abs(dev)


def _looks_iridium(d: dict, min_conf: int, min_nsyms: int) -> bool:
    """Cheap heuristic: is this RAW: line plausibly a real Iridium burst
    (as opposed to a spur / noise trigger)?

    Three signals combined:
      - frequency within ±5 kHz of an Iridium channel centre
      - QpskDemod confidence ≥ min_conf
      - symbol count ≥ min_nsyms (below ~40 syms is almost certainly
        truncated / spurious — not enough to carry an LCW header)
    """
    if d['conf'] < min_conf:
        return False
    if d['nsyms'] < min_nsyms:
        return False
    if _channel_deviation(d['freq_hz']) > _MAX_CHAN_DEVIATION:
        return False
    return True


def _parse_input_line(line: str) -> dict:
    m = _RAW_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    d['ts_ms']    = float(d['ts'])
    d['freq_hz']  = int(d['freq'])
    d['id']       = int(d['id'])
    d['conf']     = int(d['conf'])
    d['nsyms']    = int(d['nsyms'])
    return d


def _classify(bits: str) -> tuple:
    """Return (direction, post_uw_bits) or (None, bits) if no UW near start."""
    for direction, uw in (('DL', _DL_UW), ('UL', _UL_UW)):
        # UW should be at the very start of the bits stream (that's how
        # our qpsk_demod emits RAW: lines).  Allow up to 4 bit errors.
        for lo in (0,):
            if len(bits) < lo + 24 + 24:
                continue
            got = bits[lo:lo + 24]
            hd = sum(a != b for a, b in zip(got, uw))
            if hd <= 4:
                return direction, bits[lo + 24:]
    return None, bits


def _length_bucket(nsyms: int) -> str:
    """Group by common Iridium frame-length categories."""
    if nsyms < 40:
        return '<40 (tiny)'
    if nsyms < 80:
        return '40-79'
    if nsyms < 120:
        return '80-119'
    if nsyms < 160:
        return '120-159'
    if nsyms < 180:
        return '160-179'
    if 179 <= nsyms <= 180:
        return '179-180 (typical duplex)'
    return '>180 (simplex/long)'


def _freq_band(freq_hz: int) -> str:
    """Iridium band split.  Simplex band is 1626.0-1626.5 MHz;
    everything below is duplex (voice, data, IRI, IU3, ...)."""
    if freq_hz >= 1_626_000_000:
        return 'simplex (>=1626.0 MHz)'
    if freq_hz >= 1_616_000_000:
        return 'duplex (1616-1626 MHz)'
    return 'below-band (<1616 MHz)'


def _expand_paths(paths: list) -> list:
    """Expand shell-style globs.  Falls back to the literal path if the
    caller's shell already expanded (in which case globbing an exact
    path just returns [path])."""
    out = []
    for p in paths:
        # If the path contains a glob char, use glob; otherwise pass
        # through verbatim.  This handles the common uv-run-in-zsh case
        # where zsh's nomatch behaviour passed the pattern literally.
        if any(c in p for c in '*?['):
            matches = sorted(glob.glob(p))
            if matches:
                out.extend(matches)
            else:
                print('warning: no files match {}'.format(p), file=sys.stderr)
        elif os.path.isdir(p):
            # If a directory is given, take all *.raw inside it
            out.extend(sorted(glob.glob(os.path.join(p, '*.raw'))))
        else:
            out.append(p)
    return out


def analyze(paths: list, top_n: int = 15, pattern_bits: int = 32,
            min_conf: int = 40, min_nsyms: int = 40, filter_iridium: bool = True):
    paths = _expand_paths(paths)
    if not paths:
        print('No input files.  Pass one or more .raw log files (or a '
              'directory containing them).', file=sys.stderr)
        sys.exit(2)
    parse_line = _import_parser()

    total = 0
    typed_counts = Counter()
    raw_by_dir = Counter()
    raw_by_lcw_bits = Counter()   # first 24 bits after UW
    raw_by_length = Counter()
    raw_by_band = Counter()
    raw_pattern_examples: dict = defaultdict(list)
    raw_freqs_by_pattern: dict = defaultdict(list)
    # Iridium-quality filter stats
    raw_total_all = 0             # all RAW lines from the parser
    raw_filter_rejects = Counter()   # {'off_channel': N, 'low_conf': N, 'short': N}

    for path in paths:
        with open(path) as f:
            for line in f:
                if not line.startswith('RAW: '):
                    continue
                total += 1
                d = _parse_input_line(line)
                if d is None:
                    typed_counts['UNPARSEABLE'] += 1
                    continue
                parsed = parse_line(line)
                if parsed is None:
                    typed_counts['PARSER-EXC'] += 1
                    continue
                type_code = parsed[:3]
                typed_counts[type_code] += 1
                if type_code != 'RAW':
                    continue
                raw_total_all += 1
                # Apply Iridium-quality filter before bucketing patterns
                if filter_iridium:
                    if d['conf'] < min_conf:
                        raw_filter_rejects['low_conf'] += 1
                        continue
                    if d['nsyms'] < min_nsyms:
                        raw_filter_rejects['too_short'] += 1
                        continue
                    if _channel_deviation(d['freq_hz']) > _MAX_CHAN_DEVIATION:
                        raw_filter_rejects['off_channel'] += 1
                        continue
                # Bucket the RAW bursts that passed the filter
                direction, post_uw = _classify(d['bits'])
                raw_by_dir[direction or 'noUW'] += 1
                raw_by_length[_length_bucket(d['nsyms'])] += 1
                raw_by_band[_freq_band(d['freq_hz'])] += 1
                if post_uw and len(post_uw) >= pattern_bits:
                    pat = '{}:{}'.format(direction or '??',
                                          post_uw[:pattern_bits])
                    raw_by_lcw_bits[pat] += 1
                    if len(raw_pattern_examples[pat]) < 3:
                        raw_pattern_examples[pat].append(d)
                    raw_freqs_by_pattern[pat].append(d['freq_hz'])

    # ── Report ──────────────────────────────────────────────────────────
    print('=' * 72)
    print('Iridium RAW pattern analysis')
    print('=' * 72)
    print()
    print('Total input lines:      {}'.format(total))
    print()
    print('Classification breakdown:')
    for code, cnt in typed_counts.most_common():
        pct = 100.0 * cnt / max(1, total)
        print('  {:>15s}  {:>6d}  {:5.1f}%'.format(code, cnt, pct))
    print()

    raw_total = typed_counts.get('RAW', 0)
    if raw_total == 0:
        print('No RAW frames to analyse — parser typed everything.')
        return

    # Report the filter's effect
    kept = raw_total - sum(raw_filter_rejects.values())
    if filter_iridium:
        print('RAW frames: {} total, {} kept after Iridium-quality filter'.format(
            raw_total, kept))
        print('  (--no-filter to see all RAW frames)')
        if raw_filter_rejects:
            print('  filter rejected:')
            for reason, cnt in raw_filter_rejects.most_common():
                if reason == 'low_conf':
                    hint = 'conf < {}%'.format(min_conf)
                elif reason == 'too_short':
                    hint = 'nsyms < {}'.format(min_nsyms)
                elif reason == 'off_channel':
                    hint = 'freq > ±{} Hz from any Iridium channel'.format(
                        _MAX_CHAN_DEVIATION)
                else:
                    hint = reason
                print('    {:>4d}  {}'.format(cnt, hint))
        print()
    else:
        print('RAW frames: {} (filter disabled)'.format(raw_total))
        print()

    if kept == 0:
        print('All RAW frames failed the Iridium-quality filter — most likely')
        print('the RAW output is dominated by spurs / noise.  Re-run with')
        print('--no-filter to see the ungrouped patterns anyway.')
        return
    print('  By direction:')
    for k, v in raw_by_dir.most_common():
        print('    {:>6s}  {}'.format(k, v))
    print()
    print('  By length bucket (nsyms):')
    for k, v in raw_by_length.most_common():
        print('    {:>28s}  {}'.format(k, v))
    print()
    print('  By frequency band:')
    for k, v in raw_by_band.most_common():
        print('    {:>26s}  {}'.format(k, v))
    print()

    print('Top-{} recurring {}-bit patterns after UW (candidate new message types):'
          .format(top_n, pattern_bits))
    print()
    print('  {:<40s}  {:>5s}  {:>8s}  {}'.format(
        'DIR:pattern-after-UW (first {} bits)'.format(pattern_bits),
        'count', 'freq/MHz',
        'sample ts,freq'))
    print('  ' + '-' * (40 + 5 + 8 + 15))
    for pat, cnt in raw_by_lcw_bits.most_common(top_n):
        # Show the frequency spread of this pattern — if it clusters
        # tightly it's more likely a single message type; if scattered,
        # it might just be noise coincidence.
        freqs = raw_freqs_by_pattern[pat]
        fmed = sorted(freqs)[len(freqs) // 2] / 1e6
        fspread = (max(freqs) - min(freqs)) / 1e6 if len(freqs) > 1 else 0.0
        examples = raw_pattern_examples[pat]
        ex_note = '  '.join(
            '{:.0f}ms@{:.3f}'.format(ex['ts_ms'], ex['freq_hz'] / 1e6)
            for ex in examples[:2])
        print('  {:<40s}  {:>5d}  {:>4.3f}±{:.2f}  {}'.format(
            pat[:40], cnt, fmed, fspread, ex_note))
    print()
    print('Guidance: a pattern with count >> 1 and a tight frequency spread')
    print('(≤ 5 MHz) is a strong candidate for a new message class.  Compare')
    print('its bits with iridium-toolkit\'s FORMAT.md and bitsparser.upgrade()')
    print('to see whether adding a new IridiumXYZMessage subclass would help.')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('paths', nargs='+', help='iridium-*.raw log files')
    p.add_argument('--top', type=int, default=15,
                   help='Number of top patterns to show (default 15)')
    p.add_argument('--bits', type=int, default=32,
                   help='Bit width of the pattern key (default 32)')
    p.add_argument('--min-conf', type=int, default=40,
                   help='Iridium filter: minimum QpskDemod confidence %% '
                        '(default 40, lower = more permissive)')
    p.add_argument('--min-nsyms', type=int, default=40,
                   help='Iridium filter: minimum symbols per burst '
                        '(default 40, below this is likely truncated / spur)')
    p.add_argument('--no-filter', action='store_true',
                   help='Disable Iridium-quality filter (analyse ALL RAW '
                        'frames including obvious noise triggers)')
    args = p.parse_args()
    analyze(args.paths, top_n=args.top, pattern_bits=args.bits,
            min_conf=args.min_conf, min_nsyms=args.min_nsyms,
            filter_iridium=not args.no_filter)


if __name__ == '__main__':
    main()
