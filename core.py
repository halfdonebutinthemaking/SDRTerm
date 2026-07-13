import numpy as np
from dataclasses import dataclass, field
from typing import Optional

# ── constants ─────────────────────────────────────────────────────────────────
CENTER_HZ  = 105.8e6
FFT_BINS   = 4096
DB_MAX     = 0.0
DB_MIN     = -110.0
DB_RANGE   = DB_MAX - DB_MIN
LABEL_W    = 7
REFRESH_S  = 0.15
N_AVG      = 8
GAIN_MIN   = 0.0
GAIN_MAX   = 49.6
GAIN_STEP  = 0.5
GAIN_DEF   = 0.0
BW_STEPS   = [250_000, 1_024_000, 1_400_000, 1_800_000, 2_048_000, 2_400_000]
AUDIO_RATE = 48_000
# rtlsdr_read_sync triggers LIBUSB_ERROR_OVERFLOW above a hardware-dependent
# limit.  16 384 samples (32 768 bytes) matches one librtlsdr async-callback
# frame and is reliably safe across all sample rates on macOS.
READ_MAX   = 16_384
WINDOW     = np.hanning(FFT_BINS)
FM_BW_MIN  = 30_000
FM_BW_MAX  = 200_000
FM_BW_STEP = 10_000


# ── helpers ───────────────────────────────────────────────────────────────────
def fmt_freq(hz):
    if abs(hz) >= 1e6:   return '{:.3f} MHz'.format(hz / 1e6)
    elif abs(hz) >= 1e3: return '{:.3f} kHz'.format(hz / 1e3)
    else:                return '{:.0f} Hz'.format(hz)


def parse_freq(s):
    s = s.strip()
    if not s:
        return None
    try:
        if   s[-1] in ('M', 'm'): return float(s[:-1]) * 1e6
        elif s[-1] in ('K', 'k'): return float(s[:-1]) * 1e3
        else:                     return float(s)
    except ValueError:
        return None


def correct_iq(samples):
    samples = samples - np.mean(samples)
    i, q  = samples.real.copy(), samples.imag.copy()
    i_pwr = np.mean(i ** 2)
    q_pwr = np.mean(q ** 2)
    if q_pwr > 0: q *= np.sqrt(i_pwr / q_pwr)
    if i_pwr > 0: q -= (np.mean(i * q) / i_pwr) * i
    return i + 1j * q


# ── AppState ──────────────────────────────────────────────────────────────────
@dataclass
class AppState:
    center_hz:       float         = CENTER_HZ
    bw_idx:          int           = len(BW_STEPS) - 1
    gain_db:         float         = GAIN_DEF
    gain_auto:       bool          = False
    iq_corr:         bool          = False
    gain_mode:       bool          = False
    freq_input:      Optional[str] = None
    quit:            bool          = False
    active_decoders: set           = field(default_factory=lambda: {'spectrum'})
    fm_bw_hz:          int           = 100_000
    tab_idx:           int           = 0     # 0=core, N=Nth enabled plugin
    menu_cursor:       int           = 0
    menu_active:       Optional[set] = None  # None=closed; set=pending enabled set
    path_input:        Optional[str] = None  # None=closed; str=collecting input
    path_input_target: Optional[str] = None  # plugin name that opened the input
    pending_sr:        Optional[int] = None  # sample-rate change queued by active plugin

    @property
    def bw_hz(self) -> int:
        return BW_STEPS[self.bw_idx]


# ── Decoder base ──────────────────────────────────────────────────────────────
class Decoder:
    name:            str = ''   # unique ID used as registry key
    key:             str = ''   # keyboard letter that toggles this plugin ('' = always-on)
    key_help:        str = ''   # shown in footer help line
    min_sample_rate: int = 250_000

    def start(self, state: AppState) -> None:       pass
    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None):   return None
    def stop(self) -> None:                         pass

    # optional hooks — return None / False to opt out
    def handle_key(self, key: int, state: AppState, sdr) -> bool: return False
    def status_text(self, state: AppState, result: dict): return None
    def band_columns(self, state: AppState, freq_min: float,
                     freq_range: float, plot_w: int):     return None


# ── Device base ──────────────────────────────────────────────────────────────
class Device:
    """Hardware SDR interface. Subclasses in devices/ are auto-discovered.
    open() is called once at startup; return False if hardware is unavailable.
    Subclasses must also expose sample_rate, center_freq, gain as properties.

    Optional UI hooks (core-tab only):
      key_help   — shortcut hint shown in the core footer rhs
      handle_key — called for unhandled keys on the core tab; return True to consume
      status_text — short status string shown in the core footer lhs after [IQ]
    """
    name: str     = ''
    key_help: str = ''

    def open(self) -> bool:             return False
    def close(self) -> None:            pass
    def read_samples_async(self, callback, num_samples: int) -> None: pass
    def cancel_read_async(self) -> None: pass
    def handle_key(self, key: int, state: 'AppState') -> bool: return False
    def status_text(self, state: 'AppState') -> str: return ''


# ── registry helpers ──────────────────────────────────────────────────────────
def _required_bw(names: set, registry: dict) -> int:
    if not names:
        return BW_STEPS[0]
    return max(registry[n].min_sample_rate for n in names)


def _nearest_bw(rate: int) -> int:
    for step in BW_STEPS:
        if step >= rate:
            return step
    return BW_STEPS[-1]


def toggle_decoder(name: str, registry: dict, state: AppState, sdr) -> None:
    if name in state.active_decoders:
        registry[name].stop()
        state.active_decoders.discard(name)
    else:
        needed = _required_bw(state.active_decoders | {name}, registry)
        new_bw = _nearest_bw(needed)
        if new_bw > state.bw_hz:
            state.bw_idx    = BW_STEPS.index(new_bw)
            sdr.sample_rate = new_bw
        registry[name].start(state)
        state.active_decoders.add(name)
