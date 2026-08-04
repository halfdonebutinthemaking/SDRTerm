"""End-to-end pipeline mimicking iridium-extractor's CLI, using the
Python 3 port of the extractor-python detector + demod chain.

Usage:
    python -m plugins.iridium_decoder.toolkit.run_extractor \\
        -f cu8 -r 2000000 -c 1621250000 -d 14 <file.cu8>

Output on stdout:
    RAW: <basename> <timestamp/µs> <freq/Hz> <access> <lead_out> ...
matches iridium-extractor's format so it can be piped straight into
iridium-parser.py without modification.
"""
import argparse
import os
import re
import sys
import time
from functools import partial

from . import iridium
from .detector import Detector
from .cut_and_downmix import CutAndDownmix, DownmixError
from .demod import Demod


def _process_burst(cad, dem, basename, time_stamp, signal_strength,
                   bin_index, freq, signal):
    """Called by the detector for each burst.  Runs cut_and_downmix +
    demod and prints a RAW: line matching iridium-extractor's format."""
    try:
        aligned, real_freq, direction = cad.cut_and_downmix(
            signal=signal, search_offset=int(freq))
    except DownmixError as e:
        # Silently skip un-alignable bursts (same as iridium-toolkit).
        return
    except Exception as e:
        print("DOWNMIX-ERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return

    try:
        dataarray, data, access_ok, lead_out_ok, confidence, level, nsymbols = \
            dem.demod(aligned, direction=direction)
    except Exception as e:
        print("DEMOD-ERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return

    # RAW: <basename> <ts/us> <freq> A:<ok> L:<ok> <conf>% <level> <nsyms> <bits>
    print("RAW: %s %010.4f %010d A:%s L:%s %3d%% %.5f %3d %s" % (
        basename,
        time_stamp * 1000,       # ms → µs for parity with iridium-extractor
        int(real_freq),
        "OK" if access_ok else "no",
        "OK" if lead_out_ok else "no",
        int(confidence),
        level,
        nsymbols - iridium.UW_LENGTH,
        data,
    ))
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(
        description='Python 3 port of iridium-toolkit extractor')
    parser.add_argument('-f', '--format', required=True,
                        choices=['cu8', 'ci8', 'ci16_le', 'cf32_le', 'rtl',
                                 'hackrf', 'sc16', 'float', 'fc32', 'cfile'],
                        help='Sample format of the input file')
    parser.add_argument('-r', '--sample-rate', type=int, required=True,
                        help='Sample rate of the input in Hz')
    parser.add_argument('-c', '--center', type=int, required=True,
                        help='Centre frequency of the input in Hz')
    parser.add_argument('-d', '--threshold', type=float, default=8.5,
                        help='Peak detection threshold in dB above noise floor')
    parser.add_argument('-s', '--speed', type=int, default=1,
                        help='Only calculate every N-th FFT frame')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('filename', help='Input file (or - for stdin)')
    args = parser.parse_args()

    fft_peak = pow(10, args.threshold / 10)   # dB → linear ratio

    detector = Detector(sample_rate=args.sample_rate, fft_peak=fft_peak,
                        sample_format=args.format, search_size=args.speed,
                        verbose=args.verbose)

    cad = CutAndDownmix(center=args.center, input_sample_rate=args.sample_rate,
                        verbose=args.verbose)
    dem = Demod(sample_rate=cad.output_sample_rate, verbose=args.verbose)

    basename = re.sub(r'\.[^.]*$', '',
                      os.path.basename(args.filename) if args.filename != '-'
                      else 'stdin')
    basename = "i-%.4f-t1" % time.time() if args.filename == '-' else basename

    signals = detector.process_file(
        args.filename,
        partial(_process_burst, cad, dem, basename))

    print("Done. %d peaks detected." % signals, file=sys.stderr)


if __name__ == '__main__':
    main()
