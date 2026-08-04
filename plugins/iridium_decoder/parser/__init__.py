"""Vendored copy of iridium-toolkit's message parser (BSD 2-clause).

Modules are imported flat (``import bitsparser``, ``import rs`` …) which
is how the upstream code references them.  To make that work when this
package is inside SDRTerm's namespace, we insert this directory into
``sys.path`` at import time.

Usage:

    from plugins.iridium_decoder.parser import parse_line

    text = parse_line("RAW: live ... <bits> ...")
    # text is like "IRI: u-live-e240 000006309.2823 1620784926 ..."
    # or None if the line couldn't be parsed
"""
import os
import sys
import fileinput

# Ensure our directory is importable so `import bitsparser` works from
# inside the vendored files (they use flat imports).  This is a no-op
# if we've already run.
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

# bitsparser.Message.__init__ calls fileinput.lineno(), which is only
# defined when a fileinput.input() context is active.  We're calling it
# programmatically (no fileinput), so patch in a counter so it doesn't
# crash on RuntimeError.
_lineno_counter = [0]
_original_lineno = fileinput.lineno
def _safe_lineno():
    try:
        return _original_lineno()
    except RuntimeError:
        _lineno_counter[0] += 1
        return _lineno_counter[0]
fileinput.lineno = _safe_lineno

# Now the vendored modules can find each other.
import bitsparser  # noqa: E402
from types import SimpleNamespace


# Emulate `iridium-parser.py --uw-ec --harder -` argument defaults.
# bitsparser reads options off a module-global `args` populated by
# iridium-parser's argparse; when we drive it programmatically we
# supply the same fields ourselves.
_DEFAULT_ARGS = SimpleNamespace(
    uwec        = True,     # --uw-ec  : error-correct the unique word
    harder      = True,     # --harder : try more aggressive BCH decode
    perfect     = False,
    channelize  = False,
    freqclass   = False,
    forcetype   = None,
    errorfile   = None,
    errorstats  = None,
    linefilter  = {'type': 'All', 'attr': None, 'check': None},
    dosatclass  = False,
    filter      = None,
    do_stats    = False,
    plotarg     = None,
    ofmt        = None,
    output      = 'line',
)
bitsparser.set_opts(_DEFAULT_ARGS)


def parse_line(raw_line: str) -> str:
    """Parse a single RAW: line into a typed message string.

    Returns the pretty-printed message (matches iridium-parser.py's
    default `line` output), or None on error.
    """
    line = raw_line.strip()
    if not line or not line.startswith('RAW: '):
        return None
    try:
        msg = bitsparser.Message(line).upgrade()
    except Exception:
        return None
    try:
        if getattr(msg, 'error', False):
            return msg.pretty() + ' ERR:' + ', '.join(getattr(msg, 'error_msg', []))
        return msg.pretty()
    except Exception:
        return None


__all__ = ['parse_line', 'bitsparser']
