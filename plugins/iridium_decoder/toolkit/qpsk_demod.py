"""Python 3 port of gr-iridium/lib/iridium_qpsk_demod_impl.cc.

Consumes aligned frame samples from burst_downmix and produces
demodulated bits.  Chain:
  1. Decimate to symbol rate (take every sps-th sample)
  2. First-order QPSK PLL (from gr-burst) to remove residual CFO/phase
  3. QPSK hard-decision slicer per sample
  4. Confidence based on angular offset from ideal quadrant centres
  5. Check DL / UL UW at start of symbol stream (allow HD ≤ 2)
  6. If UW OK: differential decode + Gray-map to bits
"""
import math
import numpy as np

from . import iridium

M_SQRT1_2 = 1.0 / math.sqrt(2.0)


class QpskDemod:
    def __init__(self, alpha: float = 1.0 / 5.0):
        self.alpha = alpha
        self.n_handled = 0
        self.n_access_ok_bursts = 0
        self.n_access_ok_sub_bursts = 0
        self._last_burst_id = -1

    # ── First-order PLL (from gr-burst/synchronizer_v4_impl.cc) ──────────
    @staticmethod
    def _qpsk_first_order_pll(x: np.ndarray, alpha: float):
        """Returns (y, total_phase).  y[i] = x[i] * phiHat[i]."""
        n = len(x)
        y = np.empty(n, dtype=np.complex64)
        phi_hat = complex(1, 0)
        total_phase = 0.0
        for i in range(n):
            xi = x[i]
            yi = xi * phi_hat
            y[i] = yi
            # Nearest-quadrant hard decision
            r, im = yi.real, yi.imag
            if r >= 0 and im >= 0:
                x_hat = complex(M_SQRT1_2, M_SQRT1_2)
            elif r >= 0 and im < 0:
                x_hat = complex(M_SQRT1_2, -M_SQRT1_2)
            elif r < 0 and im < 0:
                x_hat = complex(-M_SQRT1_2, -M_SQRT1_2)
            else:
                x_hat = complex(-M_SQRT1_2, M_SQRT1_2)
            er = x_hat.conjugate() * yi
            er_mag = abs(er)
            if er_mag < 1e-30:
                continue
            phi_hat_t = er / er_mag
            # phi_hat_t^alpha, principal branch
            phase_t = math.atan2(phi_hat_t.imag, phi_hat_t.real) * alpha
            total_phase += phase_t
            factor = complex(math.cos(phase_t), math.sin(phase_t))
            phi_hat = factor.conjugate() * phi_hat
            m = abs(phi_hat)
            if m > 1e-30:
                phi_hat = phi_hat / m
        return y, total_phase

    # ── Slicer + confidence ─────────────────────────────────────────────
    @staticmethod
    def _demod_qpsk(burst: np.ndarray) -> tuple:
        """Returns (n, symbols, level, confidence).

        symbols: np.ndarray[int32] of QPSK symbol indices (0..3)
                 Mapping: (I≥0,Q≥0)→0, (I<0,Q≥0)→1, (I<0,Q<0)→2, (I≥0,Q<0)→3
        n:       number of usable symbols (may be < len(burst) if the
                 trailing part is noise-only)
        """
        n_syms = len(burst)
        if n_syms == 0:
            return 0, np.zeros(0, dtype=np.int32), 0.0, 0

        mags = np.abs(burst).astype(np.float32)
        symbols = np.empty(n_syms, dtype=np.int32)
        offsets = np.empty(n_syms, dtype=np.float32)

        max_mag = 0.0
        n = 0
        low_count = 0
        for i in range(n_syms):
            m = mags[i]
            if m > max_mag:
                max_mag = m
            r, im = burst[i].real, burst[i].imag
            if r >= 0 and im >= 0:
                symbols[i] = 0
            elif r >= 0 and im < 0:
                symbols[i] = 3
            elif r < 0 and im < 0:
                symbols[i] = 2
            else:
                symbols[i] = 1
            # Confidence estimate: how far from ideal quadrant center
            phase_deg = (math.atan2(im, r) + math.pi) * 180 / math.pi
            offsets[i] = 45 - (phase_deg % 90)
            n += 1
            # Terminate on 3 consecutive noise-level samples
            if m < max_mag / 8.0:
                low_count += 1
                if low_count == 3:
                    n -= 3
                    break
            else:
                low_count = 0

        if n <= 0:
            return 0, symbols[:0], 0.0, 0
        n_ok = int(np.sum(np.abs(offsets[:n]) <= 22))
        level = float(np.sum(mags[:n]) / n)
        confidence = int(100.0 * n_ok / n)
        return n, symbols[:n], level, confidence

    # ── UW check (Hamming distance in symbol space) ─────────────────────
    @staticmethod
    def _check_uw(symbols: np.ndarray, uw: tuple) -> bool:
        if len(symbols) < iridium.UW_LENGTH:
            return False
        diffs = 0
        for i in range(iridium.UW_LENGTH):
            d = abs(int(symbols[i]) - int(uw[i]))
            if d == 3:  # 270° = ±90°, counts as 1
                d = 1
            diffs += d
        return diffs <= 2

    # ── Differential Gray decode ────────────────────────────────────────
    @staticmethod
    def _decode_deqpsk(symbols: np.ndarray) -> np.ndarray:
        """Differential decode: bits = (s - old_sym) % 4, remap [0,2,3,1]."""
        out = np.empty(len(symbols), dtype=np.int32)
        old = 0
        for i, s in enumerate(symbols):
            s = int(s)
            b = (s - old) % 4
            if b == 0: b = 0
            elif b == 1: b = 2
            elif b == 2: b = 3
            else: b = 1
            old = s
            out[i] = b
        return out

    @staticmethod
    def _symbols_to_bits(symbols: np.ndarray) -> np.ndarray:
        """Each symbol becomes 2 bits: MSB from (sym&2), LSB from (sym&1)."""
        bits = np.empty(len(symbols) * 2, dtype=np.uint8)
        bits[0::2] = (symbols & 2) >> 1
        bits[1::2] = symbols & 1
        return bits

    # ── Top-level: process one aligned frame ────────────────────────────
    def process(self, frame: dict) -> dict:
        """Consume a PDU from burst_downmix.  Returns None if UW check
        fails (frame is dropped), else a dict:
            {
                'bits':             np.ndarray[uint8],
                'n_symbols':        int,
                'direction':        DOWNLINK or UPLINK,
                'confidence':       int (0-100),
                'level':            float,
                'center_frequency': float,
                'timestamp':        int,
                'id':               int,
                'noise':            float,
                'magnitude':        float,
            }
        """
        burst = frame['samples']
        sample_rate = frame['sample_rate']
        uw_start = frame['uw_start']
        sps = int(round(sample_rate / iridium.SYMBOLS_PER_SECOND))

        # Adjust timestamp by uw_start fraction (ns)
        timestamp = frame['timestamp'] + int(uw_start * 1e9 / int(sample_rate))

        # Decimate to symbol rate
        decimated = burst[::sps]

        # First-order PLL
        after_pll, total_phase = self._qpsk_first_order_pll(decimated, self.alpha)
        # Track CFO correction
        if len(decimated) > 0:
            cf_correction = (total_phase /
                             (len(decimated) / 25000.0) / math.pi / 2.0)
        else:
            cf_correction = 0.0
        center_frequency = frame['center_frequency'] + cf_correction

        # QPSK slicer
        n, symbols, level, confidence = self._demod_qpsk(after_pll)

        dl_ok = self._check_uw(symbols, iridium.UW_DL)
        ul_ok = self._check_uw(symbols, iridium.UW_UL)

        self.n_handled += 1

        if not dl_ok and not ul_ok:
            return None

        # Count OK bursts (dedupe by burst-id/10 since bursts split into sub-ids)
        burst_id = frame['id'] // 10
        if burst_id != self._last_burst_id:
            self._last_burst_id = burst_id
            self.n_access_ok_bursts += 1
        self.n_access_ok_sub_bursts += 1

        # Differential decode + map to bits
        differential = self._decode_deqpsk(symbols)
        bits = self._symbols_to_bits(differential)

        return {
            'bits':             bits,
            'n_symbols':        int(n),
            'direction':        iridium.UPLINK if ul_ok else iridium.DOWNLINK,
            'confidence':       int(confidence),
            'level':            float(level),
            'center_frequency': float(center_frequency),
            'timestamp':        int(timestamp),
            'id':               int(frame['id']),
            'noise':            float(frame['noise']),
            'magnitude':        float(frame['magnitude']),
        }
