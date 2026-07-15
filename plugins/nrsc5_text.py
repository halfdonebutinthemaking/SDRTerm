"""NRSC-5 HD Radio signal processor — pure Python/NumPy.

SIGNAL CHAIN:

  1. Resample         — FFT-based (scipy.signal.resample), O(N log N).
                        Runs in the background thread so the SDR callback
                        (which also drives FM audio) is never stalled.
  2. Sideband filter  — FFT → mask → IFFT; keeps SC ±356..±sc_outer only.
                        Removes the strong FM carrier (≈15 dB above the
                        digital sidebands) so it cannot dominate the CP
                        correlation and mask the OFDM structure.
  3. CP correlation   — two-peak average rejects incidental single peaks.
                        Pure noise ≈ 1.3; real NRSC-5 signal ≥ 6.
  4. OFDM FFT         — batched 2-D FFT; one GIL-release per block.
  5. Phase estimation — squared-mean trick per subcarrier:
                          s = ±A·e^(jθ)+noise  →  E[s²] = A²·e^(j2θ)
                          θ = angle(E[s²]) / 2
                        No pilot knowledge required.  Works for any
                        coherent BPSK signal.  Residual ±π per-subcarrier
                        ambiguity is consistent across all symbols.
  5½. MER             — from the phase-corrected BPSK constellation:
                          MER = 10·log10(mean(real²) / mean(imag²))
  6. Soft LLR         — real part of phase-corrected subcarrier symbols.
  7. Deinterleave     — *** PLACEHOLDER: NRSC-5-C Table 11-x permutation ***
                        Identity for now → Viterbi sees scrambled bit order.
  8. Descramble       — *** PLACEHOLDER: NRSC-5-C §11.5 LFSR ***
                        Identity for now → coded bits still PN-scrambled.
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


# ── Stage 2: Sideband bandpass filter ────────────────────────────────────────

def _sideband_only(iq: np.ndarray, sc_outer: int) -> np.ndarray:
    """Bandpass-filter IQ to the NRSC-5 digital sideband region only.

    The FM carrier at DC is typically 15 dB stronger than the NRSC-5
    digital sidebands.  Without this filter it dominates the CP correlation
    (Stage 3), pulling sync_q down toward the noise floor and preventing
    symbol timing acquisition.

    Implementation: FFT the full buffer → zero all bins outside
    SC ±_HI_A..±sc_outer → IFFT.  This is a rectangular spectral mask,
    so there is no phase distortion (just steep but ideal band edges).
    The bin correspondence follows from the fact that NRSC-5's subcarrier
    spacing (_SR/_FFT = 363.4 Hz) is also the DFT bin spacing for a
    buffer whose length is a multiple of _FFT at sample rate _SR.

    For a buffer of N samples at _SR: DFT bin k ↔ frequency k·_SR/N.
    NRSC-5 SC index m ↔ frequency m·_SR/_FFT.
    Therefore SC m ↔ DFT bin m·N/_FFT.
    """
    N = len(iq)
    if N < _FFT * 2:
        return iq
    scale = N / _FFT          # SC index m → DFT bin m × scale
    lo_a  = int(_HI_A * scale)
    lo_b  = int((sc_outer + 1) * scale)
    hi_a  = N - lo_b          # lower sideband (negative frequencies)
    hi_b  = N - lo_a
    spec  = np.fft.fft(iq)
    mask  = np.zeros(N, dtype=bool)
    mask[lo_a:lo_b] = True
    mask[hi_a:hi_b] = True
    spec[~mask] = 0.0
    return np.fft.ifft(spec).astype(np.complex64)


# ── Stage 3: CP correlation (symbol timing) ──────────────────────────────────

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
    After sideband filtering: pure noise → quality ≈ 1.3; NRSC-5 → ≥ 6.
    """
    metric = _cp_metric(iq)
    if len(metric) < _SYM:
        return 0, 0.0
    offset = int(np.argmax(metric[:_SYM]))
    mean   = float(metric.mean()) + 1e-9
    q1     = float(metric[offset]) / mean
    q2     = float(metric[min(offset + _SYM, len(metric) - 1)]) / mean
    return offset, (q1 + q2) / 2.0


# ── Stage 4: OFDM FFT (batched, GIL-free) ────────────────────────────────────

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


# ── Stage 5: Per-subcarrier BPSK phase estimation ────────────────────────────

def _estimate_phase_and_mer(hi: np.ndarray, lo: np.ndarray):
    """Per-subcarrier carrier-phase estimation using the squared-mean trick.

    For coherent BPSK each symbol carries s = ±A·e^(jθ) + noise, where θ
    is the carrier phase on that subcarrier.  Squaring removes the ±1
    data ambiguity:

        s² = A²·e^(j2θ) + cross-terms (small for large N)

    Averaging over all symbols in the block:

        E[s²] ≈ A²·e^(j2θ)   →   θ = angle(E[s²]) / 2

    Remaining ±π ambiguity (θ and θ+π both satisfy the equation) is
    consistent across all symbols on the same subcarrier, so the Viterbi
    sees correct LLR magnitudes — just possibly an overall bit-polarity
    flip per subcarrier that the descrambler (Stage 8) normally corrects.

    MER = 10·log10(mean(real²) / mean(imag²)) from the corrected
    constellation.  Pure noise → 0 dB; real BPSK → positive values.

    Returns (hi_corrected, lo_corrected, mer_db).
    """
    def _correct(sc):                               # sc: (n_sym, n_sc)
        theta = np.angle(np.mean(sc ** 2, axis=0)) / 2.0
        return (sc * np.exp(-1j * theta)).astype(np.complex64)

    hi_c = _correct(hi)
    lo_c = _correct(lo)
    both = np.concatenate([hi_c, lo_c], axis=1)
    sig  = float(np.mean(both.real ** 2))
    nse  = float(np.mean(both.imag ** 2)) + 1e-20
    return hi_c, lo_c, float(10.0 * np.log10(sig / nse))


# ── Stage 7: Block deinterleaver ─────────────────────────────────────────────

def _deinterleave(llr: np.ndarray, n_sym: int, n_sc: int) -> np.ndarray:
    """Reverse the NRSC-5 time-frequency block interleaver.

    The NRSC-5-C block interleaver (§11.4) writes coded bits into an
    n_sym × n_sc matrix ROW-BY-ROW (symbol-major), then reads it
    COLUMN-BY-COLUMN (subcarrier-major) to produce the OFDM symbol stream.

    Transmitter permutation: the coded bit at logical index i ends up at
      OFDM position (sym, sc) = (i // n_sc, i % n_sc)  → written row-by-row.
    The column-by-column read puts that bit at stream position:
      j = sc * n_sym + sym = (i % n_sc) * n_sym + (i // n_sc)

    At the receiver, our LLR array is in OFDM (symbol-major) order:
      llr[sym * n_sc + sc] = soft bit for that (sym, sc) cell.

    Deinterleaving = inverse permutation = matrix transpose:
      D[sym, sc] = llr[sym * n_sc + sc]  → shape (n_sym, n_sc)
      E = D.T.ravel()                     → shape (n_sc * n_sym,)
      E[sc * n_sym + sym] = D[sym, sc] = llr[sym * n_sc + sc]  ✓

    NOTE: the exact NRSC-5-C permutation may add a second shuffle on top of
    this transpose (see §11.4 Table 11-x in the NAB standard document).
    If text PDUs are not found after correct sync and MER, verify the
    permutation against the spec or the theori-io/nrsc5 reference source.
    """
    N = n_sym * n_sc
    D = llr[:N].reshape(n_sym, n_sc)
    return np.ascontiguousarray(D.T).ravel()


# ── Stage 8: PN descrambler ───────────────────────────────────────────────────

def _make_pn_mask(n: int) -> np.ndarray:
    """Generate n bits of the NRSC-5 PN sequence.

    9-bit maximum-length LFSR, polynomial x^9 + x^4 + 1 (NRSC-5-C §11.5).
    Feedback = state[8] XOR state[3]  (tap positions 9 and 4, 1-indexed).
    Initial state: 0x1FF (all ones) — standard initial value.

    Period = 2^9 − 1 = 511 bits.  We pre-compute one period and tile it to
    avoid a slow Python loop.
    """
    period = (1 << 9) - 1   # = 511 (max-length sequence)
    state  = 0x1FF
    one_p  = np.empty(period, dtype=np.float32)
    for i in range(period):
        fb       = ((state >> 8) ^ (state >> 3)) & 1
        one_p[i] = float(fb)
        state    = ((state << 1) | fb) & 0x1FF
    reps = -(-n // period)              # ceiling division
    return np.tile(one_p, reps)[:n]


def _descramble(llr: np.ndarray, frame_idx: int) -> np.ndarray:
    """Flip LLR signs where the NRSC-5 PN scrambler emits a '1' bit.

    XOR in the hard-bit domain = sign flip in the soft-LLR domain.
    The PN sequence is generated by a 9-bit LFSR (x^9+x^4+1, init=0x1FF)
    as defined in NRSC-5-C §11.5.

    Known limitation: the initial state may be per-frame or per-partition
    (see NRSC-5-C §11.5 for the exact initialisation rule).  If PDUs are
    not found after correct deinterleaving, try running _make_pn_mask with
    different initial states (0x000 and 0x1FF are the two likely options).
    """
    mask  = _make_pn_mask(len(llr))
    signs = 1.0 - 2.0 * mask       # 0 → +1 (no flip), 1 → -1 (flip)
    return llr * signs


# ── Plugin ────────────────────────────────────────────────────────────────────

class NRSC5TextDecoder(Decoder):
    name            = 'nrsc5_text'
    key             = 'n'
    key_help        = '[/]=shoulder'
    min_sample_rate = _MIN_SR

    def __init__(self):
        self._buf_lock  = threading.Lock()
        # Raw samples at device sample rate — no resampling in process()
        # so the SDR callback thread stays fast and FM audio is unaffected.
        self._raw_buf   = np.zeros(0, dtype=np.complex64)
        self._raw_sr    = None     # sample rate of current _raw_buf contents

        self._sc_outer  = _HI_B
        self._info_lock = threading.Lock()
        self._info = {
            'status': 'searching…',
            'name': '', 'slogan': '', 'psd': '',
            'mer': -99.0, 'sync_q': 0.0,
        }
        self._active = False
        self._event  = threading.Event()
        self._thread = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, state: AppState) -> None:
        self._active = True
        with self._buf_lock:
            self._raw_buf = np.zeros(0, dtype=np.complex64)
            self._raw_sr  = None
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
            self._raw_buf = np.zeros(0, dtype=np.complex64)

    # ── process() — SDR callback, O(N) buffer append only ────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        """Fast path: only append raw samples.  All heavy DSP is in the
        background thread so this never stalls the FM audio callback.
        """
        sr = int(state.bw_hz)
        self._sc_outer = state.nrsc5_sc_outer

        raw = samples.astype(np.complex64)
        with self._buf_lock:
            if self._raw_sr != sr:
                # Sample rate changed — flush stale buffer
                self._raw_sr  = sr
                self._raw_buf = np.zeros(0, dtype=np.complex64)
            self._raw_buf = np.concatenate([self._raw_buf, raw])
            # raw_cap: enough raw samples to resample into one NRSC-5 decode block
            _nrsc5_need = _DECODE_FRAMES * _FRAME_SYMS * _SYM + _CP
            raw_cap = int(round(_nrsc5_need * sr / _SR)) + 1
            if len(self._raw_buf) > raw_cap * 3:
                self._raw_buf = self._raw_buf[-raw_cap * 3:]
            if len(self._raw_buf) >= raw_cap:
                self._event.set()

        with self._info_lock:
            return dict(self._info)

    # ── decode loop — background thread ──────────────────────────────────────

    def _decode_loop(self) -> None:
        from scipy.signal import resample as _sp_resample
        nrsc5_need = _DECODE_FRAMES * _FRAME_SYMS * _SYM + _CP
        next_run   = 0.0
        while self._active:
            self._event.wait(timeout=0.5)
            self._event.clear()
            if not self._active:
                break
            if time.monotonic() < next_run:
                continue

            with self._buf_lock:
                sr = self._raw_sr
                if sr is None:
                    continue
                raw_need = int(round(nrsc5_need * sr / _SR)) + 1
                if len(self._raw_buf) < raw_need:
                    continue
                raw          = self._raw_buf[:raw_need].copy()
                self._raw_buf = self._raw_buf[raw_need // 2:]

            # Stage 1: Resample in background thread.
            # FFT-based resample is O(N log N) with no FIR design cost.
            # Running here (not in process()) keeps the FM audio callback fast.
            iq = _sp_resample(raw, nrsc5_need).astype(np.complex64)

            info = self._run_pipeline(iq, self._sc_outer)
            with self._info_lock:
                self._info.update(info)
            next_run = time.monotonic() + _DECODE_INTERVAL

    def _run_pipeline(self, iq: np.ndarray, sc_outer: int) -> dict:
        info = {}

        # Stage 2: Sideband bandpass filter — removes FM carrier from timing.
        # The FM carrier is ≈15 dB above the digital sidebands; without this
        # it dominates the CP metric and sync_q stays near the noise floor.
        iq_filt = _sideband_only(iq, sc_outer)

        # Stage 3: CP correlation on filtered signal → symbol timing
        offset, sync_q = _find_timing(iq_filt)
        info['sync_q'] = round(sync_q, 1)
        n_sym = (len(iq_filt) - offset) // _SYM
        if n_sym < _FRAME_SYMS:
            info['status'] = 'no sync'
            return info

        # Stage 4: OFDM FFT on sideband-filtered signal
        ffts = _ofdm_fft(iq_filt, offset, n_sym)   # (n_sym, 2048) complex64

        # Extract sideband subcarrier bins
        hi = ffts[:, _HI_A: sc_outer + 1]
        lo = ffts[:, _FFT - sc_outer: _FFT - _HI_A + 1]

        # Stage 5 + 5½: Per-subcarrier phase estimation and MER
        hi_c, lo_c, mer = _estimate_phase_and_mer(hi, lo)
        info['mer'] = round(mer, 1)

        if sync_q > 6.0 and mer > 3.0:
            info['status'] = 'locked'
        elif sync_q > 3.0 or mer > 1.0:
            info['status'] = 'syncing'
        else:
            info['status'] = 'searching…'

        # Stage 6: Soft LLR from phase-corrected BPSK constellation
        # Real part of corrected symbols → positive = likely +1, negative = likely -1
        llr = np.concatenate(
            [hi_c.real, lo_c.real], axis=1
        ).astype(np.float32).ravel()

        n_sc       = (sc_outer - _HI_A + 1) * 2
        n_sym_used = (n_sym // _FRAME_SYMS) * _FRAME_SYMS

        # Stage 7: Deinterleave  *** PLACEHOLDER — needs NRSC-5-C table ***
        llr = _deinterleave(llr, n_sym_used, n_sc)

        # Stage 8: PN descramble  *** PLACEHOLDER — needs NRSC-5-C poly ***
        llr = _descramble(llr, 0)

        # Limit Viterbi length.  Each 2 LLR values = 1 Viterbi step = 1 decoded bit.
        # 16384 LLRs → 8192 steps → 8192 source bits → 1024 decoded bytes to scan.
        # Running in background thread, so latency here doesn't affect audio.
        max_llr = 16384
        if len(llr) > max_llr:
            llr = llr[:max_llr]

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
        station = result.get('name') or result.get('id') or '—'

        lines = [
            'HD Radio  {}  MER {:+.0f} dB  sync {:.1f}'.format(
                status, mer, sync_q),
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
