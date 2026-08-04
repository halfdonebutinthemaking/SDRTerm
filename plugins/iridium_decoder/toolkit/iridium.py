"""Iridium constants — direct port of gr-iridium/lib/iridium.h."""
DOWNLINK = 0
UPLINK   = 1

SYMBOLS_PER_SECOND = 25000
UW_LENGTH          = 12

SIMPLEX_FREQUENCY_MIN = 1626000000

PREAMBLE_LENGTH_SHORT = 16
PREAMBLE_LENGTH_LONG  = 64

# Frame length in SYMBOLS (not bits)
MIN_FRAME_LENGTH_NORMAL  = 131   # IBC frame
MAX_FRAME_LENGTH_NORMAL  = 191

MIN_FRAME_LENGTH_SIMPLEX = 80    # Single page IRA
MAX_FRAME_LENGTH_SIMPLEX = 444

# Unique words in absolute QPSK symbol-index space (0..3)
UW_DL = (0, 2, 2, 2, 2, 0, 0, 0, 2, 0, 0, 2)
UW_UL = (2, 2, 0, 0, 0, 2, 0, 0, 2, 0, 2, 2)
