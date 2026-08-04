"""Python 3 port of iridium-toolkit's extractor-python.

Ground-truth reference: the gr-iridium C++ `iridium-extractor` binary.
This package is a line-by-line port of the Python 2 reference implementation
in iridium-toolkit/extractor-python/, validated bit-for-bit against gr-iridium
on a known capture.

Layout mirrors the original:
    iridium.py                — constants (DL/UL, symbol rate, UW length)
    filters.py                — RRC filter (from commpy)
    complex_sync_search.py    — pre-computed sync-word templates at fine freq
                                offsets; FFT-correlation for detection
    cut_and_downmix.py        — per-burst: shift → LPF → decimate → FFT freq
                                estimate → sync align → RRC matched filter
    demod.py                  — adaptive symbol timing → QPSK symbol
                                extraction → Gray-differential-decode to bits
    detector.py               — sliding-FFT peak detection over history-
                                averaged noise floor; extracts burst slices
    run_extractor.py          — top-level, mimics the CLI of iridium-extractor
"""
