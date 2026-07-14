"""NRSC-5 HD Radio text decoder — pure numpy/scipy.

Demodulates the IBOC digital sidebands flanking an FM carrier,
runs soft-Viterbi FEC, and extracts SIS / PSD text (station name,
slogan, title/artist).  No audio, no external library.

OFDM parameters (FM Hybrid mode, NRSC-5-C):
  Native sample rate : 744 187.5 Hz  (we resample from device rate)
  FFT size           : 2048 subcarriers
  Cyclic prefix      : 112 samples
  Symbol period      : 2160 samples  ≈ 2.90 ms → ~345 symbols/s
  Subcarrier spacing : 363.4 Hz

Primary Main sidebands occupy subcarrier indices −546…−356 (lower)
and +356…+546 (upper), placing digital energy at ±129…±198 kHz.

The decode pipeline (OFDM → differential BPSK → Viterbi → PDU parse)
runs in a background thread so it does not stall the SDR pipeline.
"""
import curses
import threading
import time
import numpy as np
from core import Decoder, AppState, LABEL_W

# ── NRSC-5 OFDM constants ─────────────────────────────────────────────────────
_SR   = 744_187         # processing sample rate (true: 744 187.5 Hz)
_FFT  = 2048
_CP   = 112
_SYM  = _FFT + _CP      # 2160 samples per symbol

# Primary Main (PM) sideband subcarrier index ranges, relative to DC
_LO_A, _LO_B = -546, -356   # lower sideband  191 subcarriers
_HI_A, _HI_B =  356,  546   # upper sideband  191 subcarriers
_N_SC = _HI_B - _HI_A + 1   # 191

# NRSC-5 Layer-1 frame: 32 OFDM symbols
_SYMS_PER_FRAME = 32

# Bandwidth requirement: ±546 SC × 363.4 Hz ≈ ±198 kHz → need at least 400 kHz
_MIN_SR = 1_024_000

# Accumulate this many symbols before running a decode pass.
# 64 symbols ≈ 2 frames ≈ 186 ms of signal at 345 sym/s.
_DECODE_SYMS = 64

# Minimum wall-clock seconds between decode passes.  Viterbi runs a Python
# loop and holds the GIL between numpy calls; rate-limiting keeps GIL
# occupation low enough that audio callbacks and the curses redraw are
# unaffected.  Text updates slowly (station name rarely changes), so 3 s is fine.
_DECODE_INTERVAL = 3.0

# Maximum bit-pairs fed to Viterbi per decode.
# 512 pairs → ~75 ms Python loop → with GIL yields every 64 steps,
# other threads get ~1 ms windows regularly throughout.
_MAX_VITERBI_PAIRS = 512

# ── Convolutional trellis (rate-1/2, K=7, poly 0o133 / 0o171) ─────────────────
_K       = 7
_NSTATES = 1 << (_K - 1)   # 64 states


def _build_trellis():
    G  = (0b1011011, 0b1111001)
    ns = np.zeros((_NSTATES, 2), dtype=np.int32)
    bm = np.zeros((_NSTATES, 2, 2), dtype=np.float32)  # (1 − 2·oi)
    for s in range(_NSTATES):
        for b in range(2):
            r        = s | (b << (_K - 1))
            o0, o1   = bin(r & G[0]).count('1') & 1, bin(r & G[1]).count('1') & 1
            ns[s, b] = (s >> 1) | (b << (_K - 2))
            bm[s, b] = [1.0 - 2.0 * o0, 1.0 - 2.0 * o1]
    ps  = np.zeros((_NSTATES, 2), dtype=np.int32)
    pb  = np.zeros((_NSTATES, 2), dtype=np.int32)
    cnt = np.zeros(_NSTATES,      dtype=np.int32)
    for s in range(_NSTATES):
        for b in range(2):
            n = ns[s, b]; ps[n, cnt[n]] = s; pb[n, cnt[n]] = b; cnt[n] += 1
    return ns, bm, ps, pb


_TNS, _TBM, _TPS, _TPB = _build_trellis()


def _viterbi(llr: np.ndarray) -> np.ndarray:
    """Soft-decision Viterbi.  llr: float32 length 2N (positive = likely-1).

    Yields the GIL via time.sleep(0) every 64 steps so that audio callbacks
    and the main thread can run during the forward pass.
    """
    N   = len(llr) // 2
    llr = llr[:2 * N].reshape(N, 2)
    met = np.full(_NSTATES, -1e9, dtype=np.float32)
    met[0] = 0.0
    sur = np.empty((N, _NSTATES), dtype=np.int32)
    for t in range(N):
        if t & 63 == 0:          # every 64 steps: yield so other threads can run
            time.sleep(0)
        l  = llr[t]
        bm = _TBM[:, :, 0] * l[0] + _TBM[:, :, 1] * l[1]   # (S, 2)
        c0 = met[_TPS[:, 0]] + bm[_TPS[:, 0], _TPB[:, 0]]
        c1 = met[_TPS[:, 1]] + bm[_TPS[:, 1], _TPB[:, 1]]
        ok  = c0 >= c1
        met = np.where(ok, c0, c1)
        sur[t] = np.where(ok, _TPS[:, 0], _TPS[:, 1])
    out   = np.empty(N, dtype=np.uint8)
    state = int(met.argmax())
    for t in range(N - 1, -1, -1):
        out[t] = (state >> (_K - 2)) & 1
        state  = int(sur[t, state])
    return out


# ── CRC-16/CCITT (used by NRSC-5 SIS PDUs) ───────────────────────────────────
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
    return crc & 0xFFFF


# ── OFDM helpers ──────────────────────────────────────────────────────────────

def _cp_metric(iq: np.ndarray) -> np.ndarray:
    """CP correlation magnitude; peaks at OFDM symbol boundaries.

    iq[n] and iq[n + FFT] are identical within the CP, so their conjugate
    product has near-constant phase over the guard interval.  Rolling the
    sum gives the peak at each symbol start.
    """
    n = len(iq) - _FFT - _CP
    if n <= 0:
        return np.zeros(1, dtype=np.float32)
    prod = iq[:n] * np.conj(iq[_FFT: _FFT + n])
    cs   = np.concatenate([[0j], np.cumsum(prod)])
    return np.abs(cs[_CP: n + 1] - cs[: n + 1 - _CP]).astype(np.float32)


def _find_offset(metric: np.ndarray) -> int:
    """Largest CP-correlation peak within the first symbol period."""
    if len(metric) == 0:
        return 0
    return int(np.argmax(metric[:_SYM]))


def _extract_subcarriers(iq: np.ndarray, offset: int, n_sym: int):
    """OFDM FFT-demodulate n_sym symbols → (lo, hi) each (n_sym, 191) complex64.

    Uses a single 2-D batch FFT so the GIL is released for the entire
    computation — no per-symbol Python loop.
    """
    end   = offset + n_sym * _SYM
    avail = min(n_sym, (len(iq) - offset) // _SYM)
    if avail <= 0:
        empty = np.zeros((0, _N_SC), dtype=np.complex64)
        return empty, empty.copy()
    end      = offset + avail * _SYM
    payloads = iq[offset:end].reshape(avail, _SYM)[:, _CP:]   # (avail, _FFT)
    ffts     = np.fft.fft(payloads, axis=1)                    # single GIL-free op
    hi = ffts[:, _HI_A: _HI_B + 1].astype(np.complex64)
    lo = ffts[:, _FFT + _LO_A: _FFT + _LO_B + 1].astype(np.complex64)
    return lo, hi


def _differential_llr(syms: np.ndarray) -> np.ndarray:
    """Differential BPSK LLR across symbol time.  syms: (N, M) complex.
    Positive output = likely-1; negative = likely-0.
    Removes carrier frequency offset up to ±182 Hz without a frequency lock.
    """
    diff = syms[1:] * np.conj(syms[:-1])
    return diff.real.astype(np.float32).ravel()


def _sideband_snr_db(syms: np.ndarray) -> float:
    """Rough SNR from magnitude statistics of demodulated symbols."""
    if syms.size == 0:
        return -99.0
    r = np.abs(syms).ravel()
    mu = float(r.mean())
    if mu < 1e-10:
        return -99.0
    noise = float(np.var(r))
    if noise < 1e-20:
        return 40.0
    return float(10.0 * np.log10(mu ** 2 / noise))


# ── SIS / PSD text extraction ─────────────────────────────────────────────────
# NRSC-5-C SIS PDU (§4.8, simplified):
#   byte 0      : type (lower nibble) | seq (upper nibble)
#   bytes 1..N  : ASCII payload, null-terminated
#   last 2 bytes: CRC-16
_PDU_ID    = 0x01   # short station name (4 chars, RBDS call sign style)
_PDU_NAME  = 0x04   # long station name
_PDU_SLOGAN = 0x05
_PDU_PSD   = 0x07   # program service data (artist / title via ID3 frames)
_PDU_LABELS = {_PDU_ID: 'id', _PDU_NAME: 'name', _PDU_SLOGAN: 'slogan', _PDU_PSD: 'psd'}


def _parse_pdu(raw: bytes) -> dict:
    """Validate CRC and extract text from a candidate SIS PDU."""
    if len(raw) < 4 or _crc16(raw) != 0:
        return {}
    pdu_type = raw[0] & 0x0F
    text     = raw[1:-2].split(b'\x00')[0]
    try:
        s = text.decode('ascii').strip()
    except UnicodeDecodeError:
        return {}
    if len(s) < 1 or not any(c.isprintable() and not c.isspace() for c in s):
        return {}
    label = _PDU_LABELS.get(pdu_type)
    return {label: s} if label else {}


def _scan_bits(bits: np.ndarray) -> dict:
    """Slide across decoded bits; collect CRC-valid SIS PDUs."""
    result = {}
    n      = (len(bits) // 8) * 8
    raw    = bytes(np.packbits(bits[:n]))
    for plen in range(4, min(len(raw) + 1, 64)):
        for start in range(len(raw) - plen + 1):
            result.update(_parse_pdu(raw[start: start + plen]))
            if len(result) >= 4:
                return result
    return result


# ── Plugin ────────────────────────────────────────────────────────────────────

class NRSC5TextDecoder(Decoder):
    name            = 'nrsc5_text'
    key             = 'n'
    key_help        = 'n=HD'
    min_sample_rate = _MIN_SR

    def __init__(self):
        self._buf_lock   = threading.Lock()
        self._iq_buf     = np.zeros(0, dtype=np.complex64)
        self._sr_cached  = None

        self._info_lock  = threading.Lock()
        self._info       = {
            'status': 'searching…',
            'id':     '', 'name':   '',
            'slogan': '', 'psd':    '',
            'snr':    -99.0, 'sync_q': 0.0,
        }
        self._active     = False
        self._event      = threading.Event()
        self._thread     = None

    # ── process() — fast path (SDR callback thread) ───────────────────────────

    def start(self, state: AppState) -> None:
        self._active = True
        with self._buf_lock:
            self._iq_buf    = np.zeros(0, dtype=np.complex64)
            self._sr_cached = None
        self._thread = threading.Thread(target=self._decode_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        self._event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._buf_lock:
            self._iq_buf = np.zeros(0, dtype=np.complex64)

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        from scipy.signal import resample as _sp_resample

        sr = int(state.bw_hz)
        if sr != self._sr_cached:
            self._sr_cached = sr
            with self._buf_lock:
                self._iq_buf = np.zeros(0, dtype=np.complex64)

        # FFT-based resample: O(N log N), works for any ratio with no FIR design cost.
        # resample_poly would compute a ~20M-tap FIR for the 1024000→744187 ratio.
        out_len = max(1, round(len(samples) * _SR / sr))
        rs = _sp_resample(samples, out_len).astype(np.complex64)

        with self._buf_lock:
            self._iq_buf = np.concatenate([self._iq_buf, rs])
            # Signal decode thread when we have enough symbols
            if len(self._iq_buf) >= _DECODE_SYMS * _SYM + _CP:
                self._event.set()

        with self._info_lock:
            return dict(self._info)

    # ── decode loop — background thread ───────────────────────────────────────

    def _decode_loop(self) -> None:
        need       = _DECODE_SYMS * _SYM + _CP
        next_decode = 0.0          # wall-clock time of next allowed decode pass
        while self._active:
            self._event.wait(timeout=0.5)
            self._event.clear()
            if not self._active:
                break

            now = time.monotonic()
            if now < next_decode:
                continue           # rate-limit: don't decode yet

            with self._buf_lock:
                if len(self._iq_buf) < need:
                    continue
                iq           = self._iq_buf[:need].copy()
                self._iq_buf = self._iq_buf[need // 2:]   # 50 % overlap

            new_info = self._decode_block(iq)
            with self._info_lock:
                self._info.update(new_info)
            next_decode = time.monotonic() + _DECODE_INTERVAL

    def _decode_block(self, iq: np.ndarray) -> dict:
        info = {}

        # 1. Symbol timing via CP correlation
        metric  = _cp_metric(iq)
        offset  = _find_offset(metric)
        peak    = float(metric[offset])
        mean    = float(metric.mean()) + 1e-9
        sync_q  = peak / mean
        info['sync_q'] = sync_q

        n_sym = (len(iq) - offset) // _SYM
        if n_sym < 4:
            info['status'] = 'no sync'
            return info

        # 2. OFDM demodulation — extract PM sideband subcarriers
        lo, hi = _extract_subcarriers(iq, offset, n_sym)
        both   = np.concatenate([lo, hi], axis=1)   # (n_sym, 382)

        # 3. Signal quality check
        snr = _sideband_snr_db(both)
        info['snr']    = snr
        info['status'] = 'locked' if sync_q > 3.0 else 'syncing'

        if snr < -10.0:
            info['status'] = 'weak signal'
            return info

        # 4. Differential BPSK → soft LLR values (one per bit-pair)
        llr = _differential_llr(both)
        if len(llr) > _MAX_VITERBI_PAIRS * 2:
            llr = llr[:_MAX_VITERBI_PAIRS * 2]

        # 5. Viterbi FEC
        bits = _viterbi(llr)

        # 6. Scan decoded bits for CRC-valid SIS PDUs
        text = _scan_bits(bits)
        info.update(text)
        return info

    # ── display hooks ─────────────────────────────────────────────────────────

    def status_text(self, state: AppState, result: dict) -> str:
        return '[HD:{} {:.0f}dB] '.format(
            result.get('status', '?'), result.get('snr', -99.0))

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        if height < 6:
            return

        # Tint the digital sideband columns
        if curses.has_colors():
            cf   = state.center_hz
            sc_w = _SR / _FFT   # Hz per subcarrier ≈ 363.4 Hz
            for a, b in ((_LO_A, _LO_B), (_HI_A, _HI_B)):
                col_l = int(max(0, (cf + a * sc_w - freq_min) / freq_range * plot_w))
                col_r = int(min(plot_w, (cf + b * sc_w - freq_min) / freq_range * plot_w))
                if col_r <= col_l:
                    continue
                attr = curses.color_pair(1)
                for r in range(height - 5):
                    try:
                        screen_obj.chgat(r + 1, LABEL_W + col_l,
                                         col_r - col_l, attr)
                    except curses.error:
                        pass

        # Bottom 5 body rows: decoded text
        status = result.get('status', 'searching…')
        snr    = result.get('snr',    -99.0)
        sync_q = result.get('sync_q', 0.0)

        station = result.get('name') or result.get('id') or '—'
        lines = [
            'HD Radio  {}  SNR {:+.0f} dB  sync {:.1f}'.format(
                status, snr, sync_q),
            'Station: {}'.format(station),
            'Slogan:  {}'.format(result.get('slogan') or '—'),
            'Now:     {}'.format(result.get('psd')    or '—'),
            '─' * min(plot_w, 60),
        ]

        base = height - 5   # first overlay row (0-indexed body)
        for i, line in enumerate(lines):
            row = base + i
            if row < 0 or row >= height:
                continue
            try:
                screen_obj.addstr(row + 1, LABEL_W,
                                  line[:plot_w].ljust(plot_w)[:plot_w],
                                  curses.A_BOLD if i < 4 else curses.A_DIM)
            except curses.error:
                pass
