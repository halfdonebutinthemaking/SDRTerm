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


def analyze(paths: list, top_n: int = 15, pattern_bits: int = 32):
    parse_line = _import_parser()

    total = 0
    typed_counts = Counter()
    raw_by_dir = Counter()
    raw_by_lcw_bits = Counter()   # first 24 bits after UW
    raw_by_length = Counter()
    raw_by_band = Counter()
    raw_pattern_examples: dict = defaultdict(list)
    raw_freqs_by_pattern: dict = defaultdict(list)

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
                # Bucket the RAW bursts
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

    print('RAW frames: {}'.format(raw_total))
    print()
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
    args = p.parse_args()
    analyze(args.paths, top_n=args.top, pattern_bits=args.bits)


if __name__ == '__main__':
    main()
