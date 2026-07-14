"""NRSC-5 HD Radio signal processor — pure Python/NumPy.

SIGNAL CHAIN (each stage is a labelled function):

  1. Resample         — FFT-based, O(N log N), no FIR design cost
  2. CP correlation   — finds OFDM symbol timing
  3. OFDM FFT         — one batched 2-D FFT per block (GIL-free)
  4. Pilot tracking   — SC 0 (DC) is a reference carrier; used to remove
                        per-symbol carrier-phase drift before coherent detect
  5. Coherent BPSK    — real part of (subcarrier × conj(pilot)) gives LLR
                        (replaces the old differential approach which
                         accumulated cross-symbol phase errors)
  6. MER estimate     — signal² / noise² from real vs imag constellation arms
  7. Deinterleave     — block deinterleaver spanning 32 symbols × 382 SC.
                        *** NEEDS NRSC-5-C TABLE 11-8 permutation ***
                        Currently identity → Viterbi input is scrambled.
  8. Descramble       — XOR with 9-bit LFSR PN sequence.
                        *** NEEDS NRSC-5-C poly + init per frame ***
                        Currently identity → coded bits still scrambled.
  9. Viterbi          — rate-½, K=7, G=(0o133, 0o171), vectorised ACS
 10. SIS PDU scan     — slide over decoded bytes; validate CRC-16/CCITT

OFDM parameters (FM Hybrid mode, NRSC-5-C §11):
  Native sample rate : 744 187.5 Hz
  FFT size           : 2048 subcarriers
  Cyclic prefix      : 112 samples
  Symbol period      : 2160 samples  (~345 symbols/s)
  Subcarrier spacing : 363.4 Hz
  Primary sidebands  : SC ±356…±546 (191 SC each, 382 total)
  Frame              : 32 OFDM symbols (~92.8 ms)
"""
import curses
import threading
import time
import numpy as np
from core import Decoder, AppState, LABEL_W

# ── NRSC-5 OFDM constants ─────────────────────────────────────────────────────
_SR   = 744_187
_FFT  = 2048
_CP   = 112
_SYM  = _FFT + _CP        # 2160 samples / symbol

_HI_A, _HI_B =  356,  546   # upper primary sideband SC range
_FRAME_SYMS   = 32           # OFDM symbols per Layer-1 frame
_MIN_SR        = 1_024_000

# How many full frames to accumulate before one decode pass.
# More frames → more data to scan for valid SIS PDUs.
_DECODE_FRAMES   = 4         # 4 × 32 × 2160 = 276 480 samples at 744 kHz
_DECODE_INTERVAL = 2.0       # minimum wall-clock seconds between passes

# Viterbi / convolutional code (rate-1/2, K=7)
_K       = 7
_NSTATES = 1 << (_K - 1)    # 64

# ── Stage 9 helpers: Viterbi (rate-1/2, K=7, G=0o133/0o171) ─────────────────

def _build_trellis():
    G  = (0b1011011, 0b1111001)
    ns = np.zeros((_NSTATES, 2), dtype=np.int32)
    bm = np.zeros((_NSTATES, 2, 2), dtype=np.float32)
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
    """Soft-decision Viterbi.  llr[2i], llr[2i+1] = branch metrics at step i.

    Yields the GIL every 64 steps so audio + curses threads stay responsive.
    """
    N   = len(llr) // 2
    llr = llr[:2 * N].reshape(N, 2)
    met = np.full(_NSTATES, -1e9, dtype=np.float32)
    met[0] = 0.0
    sur = np.empty((N, _NSTATES), dtype=np.int32)
    for t in range(N):
        if t & 63 == 0:
            time.sleep(0)
        l  = llr[t]
        bm = _TBM[:, :, 0] * l[0] + _TBM[:, :, 1] * l[1]
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


# ── Stage 10: CRC-16/CCITT and SIS PDU scan ──────────────────────────────────
_PDU_LABELS = {0x01: 'id', 0x04: 'name', 0x05: 'slogan', 0x07: 'psd'}


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
    return crc & 0xFFFF


def _scan_pdus(bits: np.ndarray) -> dict:
    """Slide a window over decoded bits; collect CRC-valid SIS PDUs."""
    n   = (len(bits) // 8) * 8
    raw = bytes(np.packbits(bits[:n]))
    out = {}
    for plen in range(4, min(len(raw) + 1, 64)):
        for start in range(len(raw) - plen + 1):
            chunk = raw[start: start + plen]
            if _crc16(chunk) == 0:
                pdu_type = chunk[0] & 0x0F
                label    = _PDU_LABELS.get(pdu_type)
                if label:
                    try:
                        s = chunk[1:-2].split(b'\x00')[0].decode('ascii').strip()
                        if s and any(c.isprintable() and not c.isspace() for c in s):
                            out[label] = s
                    except UnicodeDecodeError:
                        pass
            if len(out) >= 4:
                return out
    return out


# ── Stage 2: CP correlation (symbol timing) ───────────────────────────────────

def _cp_metric(iq: np.ndarray) -> np.ndarray:
    """Windowed CP correlation.  Peak at symbol start because the CP is an
    exact copy of the last _CP samples of the FFT window:
      iq[n] == iq[n + _FFT]  for  n ∈ [sym_start, sym_start + _CP)
    Sliding the sum of |iq[n]·conj(iq[n+_FFT])| over _CP samples gives a
    sharp peak at every symbol boundary.
    """
    n = len(iq) - _FFT - _CP
    if n <= 0:
        return np.zeros(1, dtype=np.float32)
    prod = iq[:n] * np.conj(iq[_FFT: _FFT + n])
    cs   = np.concatenate([[0j], np.cumsum(prod)])
    return np.abs(cs[_CP: n + 1] - cs[: n + 1 - _CP]).astype(np.float32)


def _find_timing(iq: np.ndarray):
    """Return (offset, sync_quality) — quality = two-peak average / mean.

    Using two peaks (one symbol apart) rejects incidental single-peak noise.
    Pure noise → quality ≈ 1.3; real NRSC-5 OFDM → quality ≥ 8.
    """
    metric = _cp_metric(iq)
    if len(metric) < _SYM:
        return 0, 0.0
    offset = int(np.argmax(metric[:_SYM]))
    mean   = float(metric.mean()) + 1e-9
    q1     = float(metric[offset]) / mean
    q2     = float(metric[min(offset + _SYM, len(metric) - 1)]) / mean
    return offset, (q1 + q2) / 2.0


# ── Stage 3: OFDM FFT (batched, GIL-free) ────────────────────────────────────

def _ofdm_fft(iq: np.ndarray, offset: int, n_sym: int) -> np.ndarray:
    """Remove CP and FFT-demodulate n_sym symbols.

    Returns (n_sym, _FFT) complex64 matrix.  One 2-D FFT releases the GIL
    for the entire computation — faster and more cooperative than a loop.
    """
    avail = min(n_sym, (len(iq) - offset) // _SYM)
    if avail == 0:
        return np.zeros((0, _FFT), dtype=np.complex64)
    end   = offset + avail * _SYM
    syms  = iq[offset:end].reshape(avail, _SYM)[:, _CP:]   # remove CP
    return np.fft.fft(syms, axis=1).astype(np.complex64)


# ── Stage 4: Pilot phase tracking ────────────────────────────────────────────

def _pilot_phase(ffts: np.ndarray) -> np.ndarray:
    """Extract the unit-phasor of SC 0 (DC reference carrier) per symbol.

    NRSC-5 transmits a continuous-wave reference at SC 0 with a fixed
    known amplitude.  Its phase rotates only with residual frequency error
    and channel phase drift — NOT with data.  Dividing every data subcarrier
    by this phasor removes common-mode carrier phase drift, enabling coherent
    (rather than differential) BPSK detection.

    Note: the absolute polarity (+1 or -1) of the DC pilot changes with
    the frame number according to a PN sequence defined in NRSC-5-C §11.3.
    We don't know that sequence here, so the absolute sign of the LLRs
    alternates; the Viterbi decoder sees the right magnitude but sometimes
    the wrong polarity for whole frames.  Solving this requires frame sync
    (Stage 5, below) combined with the pilot PN lookup table.
    """
    pilot = ffts[:, 0]                          # (n_sym,) complex
    mag   = np.abs(pilot) + 1e-12
    return (pilot / mag).astype(np.complex64)   # unit phasor per symbol


# ── Stage 5: Frame sync ───────────────────────────────────────────────────────

def _frame_sync(pilot_phasors: np.ndarray) -> int:
    """Find the start of a 32-symbol frame using pilot-phase periodicity.

    Within each frame, the DC pilot polarity follows a fixed PN pattern
    (same for every frame).  Between frames the pattern repeats, so
    computing the autocorrelation of the pilot phasor at lag=32 symbols
    gives a peak at multiples of the frame boundary.

    We search the first _FRAME_SYMS symbols for the offset that maximises
    this autocorrelation: the symbol with the highest 'frame-phase coherence'
    is symbol 0 of a frame.

    Without the PN lookup table (NRSC-5-C §11.3 Table 11-x) we cannot
    correct the per-symbol pilot polarity, so this gives us FRAME TIMING
    but not absolute BIT PHASE within the frame.  That correction is applied
    after the deinterleaver (Stage 7).
    """
    n = len(pilot_phasors)
    if n < _FRAME_SYMS * 2:
        return 0
    # Autocorrelation at lag = one frame
    corr = (pilot_phasors[:n - _FRAME_SYMS]
            * np.conj(pilot_phasors[_FRAME_SYMS:]))
    # Rolling sum over one frame window
    window = np.ones(_FRAME_SYMS)
    strength = np.abs(np.convolve(corr.real, window, mode='valid'))
    return int(np.argmax(strength[:_FRAME_SYMS]))


# ── Stage 5½: Sideband SNR from pilot-corrected constellation ────────────────

def _mer_db(ffts: np.ndarray, phase: np.ndarray, sc_outer: int) -> float:
    """Modulation Error Ratio from the BPSK constellation.

    After phase correction the constellation should cluster at ±A on the
    real axis with noise on the imaginary axis.
    MER = 10 log10( mean(real²) / mean(imag²) )

    This is a real signal-quality metric — distinct from the old SNR proxy
    which compared sideband power to an adjacent noise band.
    """
    hi = ffts[:, _HI_A: sc_outer + 1]
    lo = ffts[:, _FFT - sc_outer: _FFT - _HI_A + 1]
    p  = phase[:, np.newaxis]
    hi_d = hi * np.conj(p)
    lo_d = lo * np.conj(p)
    both = np.concatenate([hi_d, lo_d], axis=1)
    sig  = float(np.mean(both.real ** 2))
    nse  = float(np.mean(both.imag ** 2)) + 1e-20
    return float(10.0 * np.log10(sig / nse))


# ── Stage 6: Coherent BPSK → soft LLR ───────────────────────────────────────

def _bpsk_llr(ffts: np.ndarray, phase: np.ndarray, sc_outer: int,
               frame_off: int) -> np.ndarray:
    """Coherent BPSK soft-decisions for both sidebands.

    Returns float32 array of shape (n_usable_syms × n_sc,) where positive
    values are 'likely +1' and negative values are 'likely −1'.

    Layout: for each symbol (in frame order starting at frame_off), all
    upper-sideband subcarriers SC 356..sc_outer followed by all lower-side-
    band subcarriers SC (FFT-sc_outer)..(FFT-356), both in ascending FFT-bin
    order.  The interleaver (Stage 7) permutes this flat array.

    WHY coherent instead of differential:
      Differential BPSK (angle of s[n]×conj(s[n-1])) only works when
      consecutive symbols on the same subcarrier are INTENDED to encode the
      difference.  NRSC-5 uses COHERENT BPSK: each symbol independently
      encodes +1 or -1 relative to an absolute phase reference (the pilot).
      Differential detection introduces error propagation and doubles the
      noise variance compared to coherent.
    """
    n_sym    = ffts.shape[0]
    # Align to frame boundary so the deinterleaver sees symbol 0 first.
    start    = frame_off % n_sym
    ordered  = np.roll(ffts, -start, axis=0)
    ph_ord   = np.roll(phase, -start)
    n_usable = (n_sym // _FRAME_SYMS) * _FRAME_SYMS   # keep whole frames only

    hi = ordered[:n_usable, _HI_A: sc_outer + 1]
    lo = ordered[:n_usable, _FFT - sc_outer: _FFT - _HI_A + 1]
    p  = ph_ord[:n_usable, np.newaxis]

    hi_d = (hi * np.conj(p)).real
    lo_d = (lo * np.conj(p)).real
    # (n_usable, n_hi + n_lo) → flatten in symbol-major order
    both = np.concatenate([hi_d, lo_d], axis=1)
    return both.astype(np.float32).ravel()


# ── Stage 7: Block deinterleaver ─────────────────────────────────────────────

def _deinterleave(llr: np.ndarray, n_sym: int, n_sc: int) -> np.ndarray:
    """Reverse the NRSC-5 time-frequency block interleaver.

    The NRSC-5-C interleaver (§11.4) spreads coded bits across all n_sym
    symbols and n_sc subcarriers of a 'super-frame' (multiple L1 frames)
    to maximise time AND frequency diversity against fading.

    The permutation π(i) is defined by a formula in NRSC-5-C Table 11-x.
    We don't have that table, so this function currently returns the input
    unchanged (identity permutation).  This means the Viterbi decoder sees
    the bits in the WRONG ORDER, making its output appear random even when
    the signal is strong.

    To complete this stage, replace the body with:
        perm = _build_interleaver_table(n_sym, n_sc)   # from spec Table 11-x
        return llr[perm]

    Once implemented, the improvement in decoded BER is dramatic — typically
    going from ~50 % (random) to <1 % on a good signal.
    """
    # *** NRSC-5-C TABLE 11-x PERMUTATION GOES HERE ***
    return llr          # identity — placeholder


# ── Stage 8: PN descrambler ───────────────────────────────────────────────────

def _descramble(llr: np.ndarray, frame_idx: int) -> np.ndarray:
    """XOR the coded bit stream with the NRSC-5 PN scrambler sequence.

    The NRSC-5 scrambler is a 9-bit LFSR (polynomial and tap configuration
    defined in NRSC-5-C §11.5).  The initial state is loaded at the start
    of each super-frame from a value known to both transmitter and receiver.

    We flip the sign of LLR values where the PN sequence is '1' (XOR in
    the log-likelihood domain is a sign flip, not bit inversion after Viterbi).

    Without the polynomial and initialisation sequence we cannot generate the
    PN mask, so this function currently returns the input unchanged.

    To complete this stage:
        lfsr  = _lfsr_init(frame_idx)          # from NRSC-5-C §11.5
        mask  = _lfsr_sequence(lfsr, len(llr)) # 0/1 per coded bit
        signs = 1.0 - 2.0 * mask.astype(np.float32)
        return llr * signs
    """
    # *** NRSC-5-C §11.5 SCRAMBLER GOES HERE ***
    return llr          # identity — placeholder


# ── Plugin ────────────────────────────────────────────────────────────────────

class NRSC5TextDecoder(Decoder):
    name            = 'nrsc5_text'
    key             = 'n'
    key_help        = '[/]=shoulder'
    min_sample_rate = _MIN_SR

    def __init__(self):
        self._buf_lock   = threading.Lock()
        self._iq_buf     = np.zeros(0, dtype=np.complex64)
        self._sr_cached  = None
        self._sc_outer   = _HI_B

        self._info_lock  = threading.Lock()
        self._info = {
            'status': 'searching…',
            'name': '', 'slogan': '', 'psd': '',
            'mer': -99.0, 'sync_q': 0.0, 'frame_off': 0,
        }
        self._active = False
        self._event  = threading.Event()
        self._thread = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._active = True
        with self._buf_lock:
            self._iq_buf    = np.zeros(0, dtype=np.complex64)
            self._sr_cached = None
        self._thread = threading.Thread(
            target=self._decode_loop, name='nrsc5', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        self._event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._buf_lock:
            self._iq_buf = np.zeros(0, dtype=np.complex64)

    # ── process() — SDR callback, fast path ──────────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        from scipy.signal import resample as _sp_resample
        sr = int(state.bw_hz)
        if sr != self._sr_cached:
            self._sr_cached = sr
            with self._buf_lock:
                self._iq_buf = np.zeros(0, dtype=np.complex64)

        self._sc_outer = state.nrsc5_sc_outer

        # Stage 1: Resample to NRSC-5 native rate.
        # FFT-based resample is O(N log N) with no FIR design cost.
        # resample_poly would need a ~20M-tap FIR for the coprime 1024000/744187 ratio.
        out_n = max(1, round(len(samples) * _SR / sr))
        rs    = _sp_resample(samples, out_n).astype(np.complex64)

        with self._buf_lock:
            self._iq_buf = np.concatenate([self._iq_buf, rs])
            _cap = _DECODE_FRAMES * _FRAME_SYMS * _SYM + _CP
            if len(self._iq_buf) > _cap * 2:
                self._iq_buf = self._iq_buf[-_cap * 2:]
            if len(self._iq_buf) >= _cap:
                self._event.set()

        with self._info_lock:
            return dict(self._info)

    # ── decode loop — background thread ──────────────────────────────────────

    def _decode_loop(self) -> None:
        need     = _DECODE_FRAMES * _FRAME_SYMS * _SYM + _CP
        next_run = 0.0
        while self._active:
            self._event.wait(timeout=0.5)
            self._event.clear()
            if not self._active:
                break
            if time.monotonic() < next_run:
                continue
            with self._buf_lock:
                if len(self._iq_buf) < need:
                    continue
                iq           = self._iq_buf[:need].copy()
                self._iq_buf = self._iq_buf[need // 2:]

            info = self._run_pipeline(iq, self._sc_outer)
            with self._info_lock:
                self._info.update(info)
            next_run = time.monotonic() + _DECODE_INTERVAL

    def _run_pipeline(self, iq: np.ndarray, sc_outer: int) -> dict:
        info = {}

        # Stage 2: CP correlation → symbol timing
        offset, sync_q = _find_timing(iq)
        info['sync_q'] = round(sync_q, 1)
        n_sym = (len(iq) - offset) // _SYM
        if n_sym < _FRAME_SYMS:
            info['status'] = 'no sync'
            return info

        # Stage 3: OFDM FFT
        ffts = _ofdm_fft(iq, offset, n_sym)   # (n_sym, 2048) complex64

        # Stage 4: DC pilot phase tracking
        phase = _pilot_phase(ffts)             # (n_sym,) unit phasor

        # Stage 5: Frame sync from pilot autocorrelation
        frame_off = _frame_sync(phase)
        info['frame_off'] = frame_off

        # Stage 5½: MER and status from phase-corrected constellation
        mer = _mer_db(ffts, phase, sc_outer)
        info['mer'] = round(mer, 1)

        if sync_q > 8.0 and mer > 0.0:
            info['status'] = 'locked'
        elif sync_q > 4.0:
            info['status'] = 'syncing'
        else:
            info['status'] = 'searching…'
            return info

        # Stage 6: Coherent BPSK → soft LLR
        llr = _bpsk_llr(ffts, phase, sc_outer, frame_off)

        n_sc  = (sc_outer - _HI_A + 1) * 2   # upper + lower SC count
        n_sym_used = (n_sym // _FRAME_SYMS) * _FRAME_SYMS

        # Stage 7: Deinterleave  *** PLACEHOLDER — needs NRSC-5-C table ***
        llr = _deinterleave(llr, n_sym_used, n_sc)

        # Stage 8: PN descramble  *** PLACEHOLDER — needs NRSC-5-C poly ***
        llr = _descramble(llr, 0)

        # Limit Viterbi length to keep background thread cooperative
        max_bits = 2048
        if len(llr) > max_bits * 2:
            llr = llr[:max_bits * 2]

        # Stage 9: Viterbi FEC
        bits = _viterbi(llr)

        # Stage 10: SIS PDU scan
        text = _scan_pdus(bits)
        info.update(text)
        return info

    # ── UI hooks ──────────────────────────────────────────────────────────────

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        from core import NRSC5_SC_MIN, NRSC5_SC_MAX, NRSC5_SC_STEP
        if key == ord('['):
            state.nrsc5_sc_outer = max(NRSC5_SC_MIN,
                                       state.nrsc5_sc_outer - NRSC5_SC_STEP)
            return True
        if key == ord(']'):
            state.nrsc5_sc_outer = min(NRSC5_SC_MAX,
                                       state.nrsc5_sc_outer + NRSC5_SC_STEP)
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        sc_w   = _SR / _FFT
        sh_khz = (state.nrsc5_sc_outer - _HI_A) * sc_w / 1000
        return '[HD:{} MER:{:+.0f}dB {:.0f}kHz sync:{:.1f}] '.format(
            result.get('status', '?'),
            result.get('mer', -99.0),
            sh_khz,
            result.get('sync_q', 0.0))

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        if height < 6:
            return

        # Cyan tint on the digital sideband columns
        if curses.has_colors():
            cf       = state.center_hz
            sc_w     = _SR / _FFT
            inner_hz = _HI_A * sc_w
            outer_hz = state.nrsc5_sc_outer * sc_w
            for sign in (-1, 1):
                c_l = int(max(0, (cf + sign * inner_hz - freq_min)
                              / freq_range * plot_w))
                c_r = int(min(plot_w, (cf + sign * outer_hz - freq_min)
                              / freq_range * plot_w))
                if sign == -1:
                    c_l, c_r = c_r, c_l
                if c_r <= c_l:
                    continue
                for r in range(height - 5):
                    try:
                        screen_obj.chgat(r + 1, LABEL_W + c_l,
                                         c_r - c_l, curses.color_pair(1))
                    except curses.error:
                        pass

        # Bottom 5 body rows
        status  = result.get('status',  'searching…')
        mer     = result.get('mer',     -99.0)
        sync_q  = result.get('sync_q',  0.0)
        foff    = result.get('frame_off', 0)
        station = result.get('name') or result.get('id') or '—'

        lines = [
            'HD Radio  {}  MER {:+.0f} dB  sync {:.1f}  frame@{}'.format(
                status, mer, sync_q, foff),
            'Station: {}'.format(station),
            'Slogan:  {}'.format(result.get('slogan') or '—'),
            'Now:     {}'.format(result.get('psd')    or '—'),
            '─' * min(plot_w, 60),
        ]

        base = height - 5
        for i, line in enumerate(lines):
            row = base + i
            if row < 0 or row >= height:
                continue
            try:
                screen_obj.addstr(
                    row + 1, LABEL_W,
                    line[:plot_w].ljust(plot_w)[:plot_w],
                    curses.A_BOLD if i < 4 else curses.A_DIM)
            except curses.error:
                pass
