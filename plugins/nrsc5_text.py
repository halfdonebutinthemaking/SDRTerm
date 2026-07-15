"""NRSC-5 HD Radio signal processor — pure Python/NumPy.

Pipeline (matches theori-io/nrsc5 reference implementation):

  1. Resample         — FFT-based, runs in background thread.
  2. Sideband filter  — keeps SC ±356..±sc_outer; removes FM carrier
                        so CP correlation gives clean timing.
  3. CP correlation   — two-peak quality metric (noise≈1.3, signal≥6).
  4. OFDM FFT         — batched 2-D FFT on sideband-filtered signal.
  5. Buffer-PM fill   — extract 10 lower + 10 upper sideband partitions
                        (18 data SCs each, skip j=0 reference), interleave
                        I and Q per sample → (n_sym, 720) float32.
  6. Deinterleave     — vectorised permutation matching theori-io
                        interleaver_i(J=20, B=16, C=36, M=1, PM_V lookup)
                        + depuncture: insert 0 at every 6th position.
  7. Viterbi          — rate 1/3, K=7, generators {0133, 0171, 0165} octal;
                        tail-biting (equal initial metrics).
  8. Descramble       — 11-bit LFSR (init=0x3ff, fb=(val>>9)^val),
                        applied to DECODED BYTES (not LLRs).
  9. SIS PDU scan     — slide over bytes; validate CRC-16/CCITT.

OFDM parameters (FM Hybrid mode):
  Native sample rate : 744 187.5 Hz
  FFT size           : 2048 subcarriers
  Cyclic prefix      : 112 samples
  Symbol period      : 2160 samples
  Primary sidebands  : SC ±356…±546 (10 partitions × 19 SC each)
  Frame              : 32 OFDM symbols (~92.8 ms)
  Interleaver cycle  : 16 frames = 512 symbols (~1.49 s)
"""
import curses
import threading
import time
import numpy as np
from core import Decoder, AppState, LABEL_W

# ── NRSC-5 OFDM constants ─────────────────────────────────────────────────────
_SR    = 744_187
_FFT   = 2048
_CP    = 112
_SYM   = _FFT + _CP          # 2160 samples / symbol
_HI_A  = 356                 # inner edge of primary sideband (for overlay/sync)

# Partition structure (FM Hybrid, primary sidebands, psmi=1 default)
_PART_WIDTH   = 19    # total SCs per partition (1 ref + 18 data)
_PART_DATA    = 18    # data SCs per partition
_PM_PARTS     = 10    # partitions per sideband
_J            = 20    # total partitions (10 lower + 10 upper)
_B            = 16    # interleaver blocks
_C            = 36    # interleaver columns per partition
_BLKSZ        = 32    # OFDM symbols per block (= one L1 frame)

# LB_START = FFT/2 - 546 = 478 (fftshifted), UB_END = 1024+546 = 1570
# In numpy FFT (DC at bin 0): SC = bin for positive, SC = bin-2048 for negative.
# Lower sideband partition p, data SC j=1..18: numpy bin = 1502 + p*19 + j
# Upper sideband partition p, data SC j=1..18: numpy bin = 356 + p*19 + j
_LB_DATA_BINS = np.array([
    1502 + p * _PART_WIDTH + j
    for p in range(_PM_PARTS)
    for j in range(1, _PART_DATA + 1)
], dtype=np.int32)   # (180,)

_UB_DATA_BINS = np.array([
    356 + p * _PART_WIDTH + j
    for p in range(_PM_PARTS)
    for j in range(1, _PART_DATA + 1)
], dtype=np.int32)   # (180,)

# P1 frame sizes
_P1_DEC   = 146_176    # decoded bits after Viterbi
_P1_ENC   = 365_440    # interleaver_i input count (= _P1_DEC * 5/2)
_P1_VIT   = _P1_DEC * 3   # Viterbi input after depuncture (= 438528)

# Interleaver total buffer size
_PM_BUF   = _B * _BLKSZ * _J * _C   # = 16*32*20*36 = 368640

# PM_V permutation lookup (theori-io defines.h / decode.c)
_PM_V = np.array(
    [10, 2, 18, 6, 14, 8, 16, 0, 12, 4,
     11, 3, 19, 7, 15, 9, 17, 1, 13, 5],
    dtype=np.int32)

# Minimum wall-clock seconds between decode passes
_DECODE_INTERVAL = 4.0
_MIN_SR          = 1_024_000


# ── Module-load: precompute deinterleave LUT ─────────────────────────────────

def _build_deinterleave_luts():
    """Vectorised precomputation of interleaver_i source/destination tables.

    Returns (src, dst) where:
      src[k]  = flat index into (512, 720) buffer_pm
      dst[k]  = flat index into (438528,) Viterbi-input array
    """
    N   = _P1_ENC
    i   = np.arange(N, dtype=np.int64)

    part  = _PM_V[i % _J]                               # partition 0..19
    block = (i // _J + part * 7) % _B                   # interleaver block 0..15
    k     = i // (_J * _B)                              # column group
    row   = (k * 11) % _BLKSZ                           # row within block 0..31
    col   = (k * 11 + k // (_BLKSZ * 9)) % _C          # column within partition 0..35

    src = (block * _BLKSZ + row) * (_J * _C) + part * _C + col   # (N,) int64
    dst = i + i // 5                                     # depuncture: 5 real, 1 zero

    return src.astype(np.int32), dst.astype(np.int32)


_DINT_SRC, _DINT_DST = _build_deinterleave_luts()


def _deinterleave_p1(buf_flat: np.ndarray) -> np.ndarray:
    """Deinterleave and depuncture P1 in one vectorised step.

    buf_flat : (368640,) float32  — buffer_pm flattened row-major
    returns  : (438528,) float32  — Viterbi input (zeros at punctured positions)
    """
    out = np.zeros(_P1_VIT, dtype=np.float32)
    out[_DINT_DST] = buf_flat[_DINT_SRC]
    return out


# ── Rate-1/3 K=7 Viterbi trellis ─────────────────────────────────────────────

_K       = 7
_NSTATES = 1 << (_K - 1)   # 64
_G3      = (0b1011011, 0b1111001, 0b1110101)   # 0133, 0171, 0165 octal


def _build_trellis_r3():
    ns = np.zeros((_NSTATES, 2), dtype=np.int32)
    bm = np.zeros((_NSTATES, 2, 3), dtype=np.float32)
    for s in range(_NSTATES):
        for b in range(2):
            r = s | (b << (_K - 1))
            outs = [bin(r & g).count('1') & 1 for g in _G3]
            ns[s, b] = (s >> 1) | (b << (_K - 2))
            bm[s, b] = [1.0 - 2.0 * o for o in outs]
    ps  = np.zeros((_NSTATES, 2), dtype=np.int32)
    pb  = np.zeros((_NSTATES, 2), dtype=np.int32)
    cnt = np.zeros(_NSTATES,       dtype=np.int32)
    for s in range(_NSTATES):
        for b in range(2):
            n = ns[s, b]; ps[n, cnt[n]] = s; pb[n, cnt[n]] = b; cnt[n] += 1
    return ns, bm, ps, pb


_TNS3, _TBM3, _TPS3, _TPB3 = _build_trellis_r3()


def _viterbi_r3(llr: np.ndarray) -> np.ndarray:
    """Rate-1/3 soft-decision Viterbi. 3 LLRs per trellis step.

    Tail-biting: all initial path metrics set to 0 (equal probability).
    Yields GIL every 64 steps so audio and UI threads stay responsive.
    """
    N   = len(llr) // 3
    llr = llr[:3 * N].reshape(N, 3)
    met = np.zeros(_NSTATES, dtype=np.float32)
    sur = np.empty((N, _NSTATES), dtype=np.int32)
    bm3 = _TBM3.reshape(_NSTATES * 2, 3)   # (128, 3) for fast dot
    for t in range(N):
        if t & 63 == 0:
            time.sleep(0)
        bm = bm3.dot(llr[t]).reshape(_NSTATES, 2)   # (64, 2)
        c0 = met[_TPS3[:, 0]] + bm[_TPS3[:, 0], _TPB3[:, 0]]
        c1 = met[_TPS3[:, 1]] + bm[_TPS3[:, 1], _TPB3[:, 1]]
        ok  = c0 >= c1
        met = np.where(ok, c0, c1)
        sur[t] = np.where(ok, _TPS3[:, 0], _TPS3[:, 1])
    out   = np.empty(N, dtype=np.uint8)
    state = int(met.argmax())
    for t in range(N - 1, -1, -1):
        out[t] = (state >> (_K - 2)) & 1
        state  = int(sur[t, state])
    return out


# ── 11-bit LFSR descrambler (applied to decoded bytes) ───────────────────────

def _descramble_bytes(data: bytes) -> bytes:
    """theori-io descramble(): 11-bit LFSR, init=0x3ff, fb=(val>>9)^val."""
    width = 11
    val   = 0x3ff
    out   = bytearray(len(data))
    for i, byte in enumerate(data):
        b = 0
        for j in range(8):
            bit  = ((val >> 9) ^ val) & 1
            val |= (bit << width)
            val >>= 1
            b   |= (bit << j)
        out[i] = byte ^ b
    return bytes(out)


# ── CRC-16/CCITT and SIS PDU scan ────────────────────────────────────────────

_PDU_LABELS   = {0x01: 'id', 0x04: 'name', 0x05: 'slogan', 0x07: 'psd'}
_PDU_TYPE_SET = frozenset(_PDU_LABELS)


def _build_crc16_table():
    t = [0] * 256
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if (c & 0x8000) else (c << 1)
        t[i] = c & 0xFFFF
    return t


_CRC16_TBL = _build_crc16_table()


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    tbl = _CRC16_TBL
    for b in data:
        crc = ((crc << 8) ^ tbl[(crc >> 8) ^ b]) & 0xFFFF
    return crc


def _scan_pdus(data: bytes) -> dict:
    """Slide window over decoded/descrambled bytes; collect CRC-valid SIS PDUs."""
    out  = {}
    rlen = len(data)
    for start in range(rlen - 5):
        if (data[start] & 0x0F) not in _PDU_TYPE_SET:
            continue
        pdu_type = data[start] & 0x0F
        for plen in range(6, min(33, rlen - start + 1)):
            if _crc16(data[start: start + plen]) == 0:
                label = _PDU_LABELS[pdu_type]
                try:
                    s = data[start+1: start+plen-2].split(b'\x00')[0].decode('ascii').strip()
                    if s and any(c.isprintable() and not c.isspace() for c in s):
                        out[label] = s
                except UnicodeDecodeError:
                    pass
                break
        if len(out) >= 4:
            break
    return out


# ── Sideband filter (for timing only) ────────────────────────────────────────

def _sideband_only(iq: np.ndarray, sc_outer: int) -> np.ndarray:
    N = len(iq)
    if N < _FFT * 2:
        return iq
    scale = N / _FFT
    lo_a  = int(_HI_A * scale)
    lo_b  = int((sc_outer + 1) * scale)
    hi_a  = N - lo_b
    hi_b  = N - lo_a
    spec  = np.fft.fft(iq)
    mask  = np.zeros(N, dtype=bool)
    mask[lo_a:lo_b] = True
    mask[hi_a:hi_b] = True
    spec[~mask] = 0.0
    return np.fft.ifft(spec).astype(np.complex64)


# ── CP correlation (symbol timing) ───────────────────────────────────────────

def _cp_metric(iq: np.ndarray) -> np.ndarray:
    n    = len(iq) - _FFT - _CP
    if n <= 0:
        return np.zeros(1, dtype=np.float32)
    prod = iq[:n] * np.conj(iq[_FFT: _FFT + n])
    cs   = np.concatenate([[0j], np.cumsum(prod)])
    return np.abs(cs[_CP: n + 1] - cs[: n + 1 - _CP]).astype(np.float32)


def _find_timing(iq: np.ndarray):
    metric = _cp_metric(iq)
    if len(metric) < _SYM:
        return 0, 0.0
    offset = int(np.argmax(metric[:_SYM]))
    mean   = float(metric.mean()) + 1e-9
    q1     = float(metric[offset]) / mean
    q2     = float(metric[min(offset + _SYM, len(metric) - 1)]) / mean
    return offset, (q1 + q2) / 2.0


# ── OFDM FFT ─────────────────────────────────────────────────────────────────

def _ofdm_fft(iq: np.ndarray, offset: int, n_sym: int) -> np.ndarray:
    avail = min(n_sym, (len(iq) - offset) // _SYM)
    if avail == 0:
        return np.zeros((0, _FFT), dtype=np.complex64)
    end  = offset + avail * _SYM
    syms = iq[offset:end].reshape(avail, _SYM)[:, _CP:]
    return np.fft.fft(syms, axis=1).astype(np.complex64)


# ── Buffer-PM extraction ──────────────────────────────────────────────────────

def _fill_buffer_pm(ffts: np.ndarray) -> np.ndarray:
    """Extract data subcarriers and fill buffer_pm (512×720) matching sync.c.

    Each complex OFDM sample contributes I and Q as independent soft bits
    (diagonal QPSK: both axes carry data after phase correction).

    Phase correction uses the squared-mean trick per subcarrier — valid
    for BPSK/QPSK-diagonal after the Costas loops in the full demodulator.
    Here we use a batch estimate over all available symbols.

    Returns (min(n_sym,512), 720) float32 buffer, row-major:
      row   = symbol index within interleaver cycle
      col   = partition*36 + SC_within_part*2 + IQ
              (lower sideband partitions 0-9, then upper 0-9)
    """
    n_sym = min(len(ffts), _B * _BLKSZ)   # cap at 512

    lb = ffts[:n_sym, _LB_DATA_BINS]   # (n_sym, 180) complex
    ub = ffts[:n_sym, _UB_DATA_BINS]   # (n_sym, 180) complex

    # Per-subcarrier BPSK phase correction: E[s²]=A²e^(j2θ) → θ=angle/2
    def _correct(sc):
        theta = np.angle(np.mean(sc ** 2, axis=0)) / 2.0
        return (sc * np.exp(-1j * theta)).astype(np.complex64)

    lb_c = _correct(lb)   # (n_sym, 180)
    ub_c = _correct(ub)

    # Reshape to (n_sym, 10 partitions, 18 SCs) then interleave I,Q → (n_sym, 10, 36)
    lb_iq = np.stack([lb_c.reshape(n_sym, _PM_PARTS, _PART_DATA).real,
                      lb_c.reshape(n_sym, _PM_PARTS, _PART_DATA).imag],
                     axis=-1).reshape(n_sym, _PM_PARTS, _C)
    ub_iq = np.stack([ub_c.reshape(n_sym, _PM_PARTS, _PART_DATA).real,
                      ub_c.reshape(n_sym, _PM_PARTS, _PART_DATA).imag],
                     axis=-1).reshape(n_sym, _PM_PARTS, _C)

    # Combine sidebands: (n_sym, 20, 36) → (n_sym, 720)
    return np.concatenate([lb_iq, ub_iq], axis=1).reshape(n_sym, _J * _C).astype(np.float32)


# ── MER estimate (for display only) ──────────────────────────────────────────

def _mer_from_ffts(ffts: np.ndarray, sc_outer: int) -> float:
    if len(ffts) == 0:
        return -99.0
    hi = ffts[:, _HI_A: sc_outer + 1]
    lo = ffts[:, _FFT - sc_outer: _FFT - _HI_A + 1]
    for sc in (hi, lo):
        theta = np.angle(np.mean(sc ** 2, axis=0)) / 2.0
        sc *= np.exp(-1j * theta)
    both = np.concatenate([hi, lo], axis=1)
    sig  = float(np.mean(both.real ** 2))
    nse  = float(np.mean(both.imag ** 2)) + 1e-20
    return float(10.0 * np.log10(sig / nse))


# ── Plugin ────────────────────────────────────────────────────────────────────

class NRSC5TextDecoder(Decoder):
    name            = 'nrsc5_text'
    key             = 'n'
    key_help        = '[/]=shoulder'
    min_sample_rate = _MIN_SR

    def __init__(self):
        self._buf_lock  = threading.Lock()
        self._raw_buf   = np.zeros(0, dtype=np.complex64)
        self._raw_sr    = None

        self._sc_outer  = 546   # updated from state each call
        self._bc_offset = 0     # current bc alignment attempt (0..15)
        self._bc_pass   = 0     # total decode passes (for cycling bc_offset)

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
        self._active    = True
        self._bc_offset = 0
        self._bc_pass   = 0
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
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._buf_lock:
            self._raw_buf = np.zeros(0, dtype=np.complex64)

    # ── process() — fast path: buffer append only ────────────────────────────

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        sr = int(state.bw_hz)
        self._sc_outer = state.nrsc5_sc_outer
        raw = samples.astype(np.complex64)

        with self._buf_lock:
            if self._raw_sr != sr:
                self._raw_sr  = sr
                self._raw_buf = np.zeros(0, dtype=np.complex64)
            self._raw_buf = np.concatenate([self._raw_buf, raw])
            # Need 16 frames = 512 symbols at native rate
            need_native = _B * _BLKSZ * _SYM + _CP
            raw_need = int(round(need_native * sr / _SR)) + 1
            if len(self._raw_buf) > raw_need * 2:
                self._raw_buf = self._raw_buf[-raw_need * 2:]
            if len(self._raw_buf) >= raw_need:
                self._event.set()

        with self._info_lock:
            return dict(self._info)

    # ── decode loop — background thread ──────────────────────────────────────

    def _decode_loop(self) -> None:
        from scipy.signal import resample as _sp_resample
        need_native = _B * _BLKSZ * _SYM + _CP
        next_run    = 0.0

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
                raw_need = int(round(need_native * sr / _SR)) + 1
                if len(self._raw_buf) < raw_need:
                    continue
                raw           = self._raw_buf[:raw_need].copy()
                self._raw_buf = self._raw_buf[raw_need // 2:]

            # Resample to native NRSC-5 rate
            iq = _sp_resample(raw, need_native).astype(np.complex64)

            info = self._run_pipeline(iq, self._sc_outer)
            with self._info_lock:
                self._info.update(info)

            next_run = time.monotonic() + _DECODE_INTERVAL

    # ── main pipeline ─────────────────────────────────────────────────────────

    def _run_pipeline(self, iq: np.ndarray, sc_outer: int) -> dict:
        info = {}

        # Stage 2: sideband filter → Stage 3: timing
        iq_filt        = _sideband_only(iq, sc_outer)
        offset, sync_q = _find_timing(iq_filt)
        info['sync_q'] = round(sync_q, 1)

        n_sym = (len(iq_filt) - offset) // _SYM
        if n_sym < _BLKSZ:
            info['status'] = 'no sync'
            return info

        # Stage 4: OFDM FFT (use unfiltered signal for data extraction;
        #          filtered was only needed for timing)
        ffts = _ofdm_fft(iq, offset, n_sym)   # (n_sym, 2048) complex64

        # MER display (uses broader hi/lo range including ref SCs — just for UI)
        info['mer'] = round(_mer_from_ffts(ffts, sc_outer), 1)

        sync_ok = sync_q > 6.0
        mer_ok  = info['mer'] > 0.0
        if sync_ok and mer_ok:
            info['status'] = 'locked'
        elif sync_q > 3.0:
            info['status'] = 'syncing'
        else:
            info['status'] = 'searching…'

        if not sync_ok:
            return info

        # Stage 5: fill buffer_pm (n_sym_used, 720)
        n_sym_used = (n_sym // _BLKSZ) * _BLKSZ
        if n_sym_used < _BLKSZ:
            return info

        ffts_used = ffts[:n_sym_used]
        buf_pm    = _fill_buffer_pm(ffts_used)   # (n_sym_used, 720)

        # We need exactly _B * _BLKSZ = 512 symbols for one full decode.
        if len(buf_pm) < _B * _BLKSZ:
            info['status'] = 'syncing (buffering…)'
            return info

        # Arrange into interleaver order using current bc_offset attempt.
        # bc_offset = k means our first received frame has bc=k.
        frames   = buf_pm[:_B * _BLKSZ].reshape(_B, _BLKSZ, _J * _C)
        ordered  = np.empty((_B, _BLKSZ, _J * _C), dtype=np.float32)
        bc_idxs  = (np.arange(_B) + self._bc_offset) % _B
        ordered[bc_idxs] = frames

        buf_flat = ordered.reshape(_PM_BUF)   # (368640,)

        # Stage 6: deinterleave + depuncture → (438528,)
        vit_in = _deinterleave_p1(buf_flat)

        # Stage 7: rate-1/3 Viterbi → (146176,) bits
        bits = _viterbi_r3(vit_in)

        # Stage 8: pack to bytes + 11-bit LFSR descramble
        raw_bytes    = bytes(np.packbits(bits))
        descrambled  = _descramble_bytes(raw_bytes)

        # Stage 9: SIS PDU scan
        pdus = _scan_pdus(descrambled)
        if pdus:
            # Found PDUs with current bc_offset — keep it
            info.update(pdus)
            info['status'] = 'locked'
        else:
            # Cycle to next bc_offset on next pass
            self._bc_pass  += 1
            self._bc_offset = self._bc_pass % _B

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
        return '[HD:{} MER:{:+.0f}dB {:.0f}kHz sync:{:.1f} bc:{}] '.format(
            result.get('status', '?'),
            result.get('mer', -99.0),
            sh_khz,
            result.get('sync_q', 0.0),
            self._bc_offset)

    def draw_overlay(self, screen_obj, state: AppState, result: dict,
                     freq_min: float, freq_range: float,
                     plot_w: int, height: int) -> None:
        if height < 6:
            return

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

        status  = result.get('status',  'searching…')
        mer     = result.get('mer',     -99.0)
        sync_q  = result.get('sync_q',  0.0)
        station = result.get('name') or result.get('id') or '—'

        lines = [
            'HD Radio  {}  MER {:+.0f} dB  sync {:.1f}  bc:{}'.format(
                status, mer, sync_q, self._bc_offset),
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
