#!/usr/bin/env python3
"""NRSC-5 pipeline diagnostic — run against a raw complex64 .iq file."""
import sys, argparse, time
import numpy as np
from scipy.signal import resample as sp_resample

# Pull full pipeline helpers from the plugin (deinterleave, Viterbi, HDLC)
sys.path.insert(0, '.')
from plugins.nrsc5_text import (
    _build_deinterleave_luts, _deinterleave_p1, _viterbi_r3,
    _build_lfsr_seq, _descramble_bits, _frame_pack, _scan_hdlc_frames,
    _P1_DEC, _B, _BLKSZ, _J, _C, _PM_BUF, _PM_V,
    _LB_DATA_BINS as _LB_BINS, _UB_DATA_BINS as _UB_BINS,
    _PART_DATA, _PM_PARTS
)
_DINT_SRC, _DINT_DST = _build_deinterleave_luts()
_LFSR_SEQ = _build_lfsr_seq(_P1_DEC)

_SR    = 744_187
_FFT   = 2048
_CP    = 112
_SYM   = _FFT + _CP
_HI_A  = 356
_HI_B  = 545
_PART_WIDTH = 19
_PART_DATA  = 18
_PM_PARTS   = 10
_J     = 20
_B     = 16
_C     = 36
_BLKSZ = 32

# Native subcarrier spacing (Hz) — used to convert SC numbers to Hz offsets
_SC_HZ = _SR / _FFT   # ≈ 363.4 Hz

# Physical frequency offsets for the primary sidebands
_INNER_HZ = _HI_A * _SC_HZ   # ≈ 129.4 kHz
_OUTER_HZ = _HI_B * _SC_HZ   # ≈ 198.1 kHz

_LB_DATA_BINS = np.array([
    1502 + p * _PART_WIDTH + j
    for p in range(_PM_PARTS)
    for j in range(1, _PART_DATA + 1)
], dtype=np.int32)

_UB_DATA_BINS = np.array([
    356 + p * _PART_WIDTH + j
    for p in range(_PM_PARTS)
    for j in range(1, _PART_DATA + 1)
], dtype=np.int32)


def sep(title=''):
    w = 72
    print('\n── {} {}'.format(title, '─' * max(0, w - len(title) - 4)) if title
          else '─' * w)


parser = argparse.ArgumentParser()
parser.add_argument('file')
parser.add_argument('--f',  type=float, default=90.7e6)
parser.add_argument('--bw', type=float, default=2_048_000)
args   = parser.parse_args()
if args.f < 1e6:
    args.f *= 1e6
CENTER = args.f
SR     = args.bw

print('File   : {}'.format(args.file))
print('Centre : {:.3f} MHz'.format(CENTER / 1e6))
print('BW     : {:.3f} MHz'.format(SR / 1e6))
print('Digital sidebands should be at ±{:.1f}..{:.1f} kHz from centre'.format(
    _INNER_HZ / 1e3, _OUTER_HZ / 1e3))

# ── 1. Load ───────────────────────────────────────────────────────────────────
sep('1. Load')
iq = np.fromfile(args.file, dtype=np.complex64)
print('Samples  : {:,}  ({:.1f} s)'.format(len(iq), len(iq) / SR))
print('Power    : {:.1f} dBFS'.format(10 * np.log10(float(np.mean(np.abs(iq)**2)) + 1e-20)))
print('DC  I/Q  : {:.4f} / {:.4f}'.format(float(iq.real.mean()), float(iq.imag.mean())))

# ── 2. Spectrum — coarse (correct frequency ranges this time) ─────────────────
sep('2. Spectrum — 65 536-pt FFT of first 65536 samples')
BLK   = 65536
block = iq[:BLK]
spec  = np.abs(np.fft.fftshift(np.fft.fft(block)))**2
freqs = np.fft.fftshift(np.fft.fftfreq(BLK, 1.0 / SR))   # relative to centre
db    = 10 * np.log10(spec / (BLK * BLK) + 1e-20)

noise_floor = float(np.percentile(db, 10))
peak_idx    = int(db.argmax())
peak_db     = float(db[peak_idx])
peak_offset = float(freqs[peak_idx])

print('Noise floor    : {:.1f} dBFS (10th percentile across full BW)'.format(noise_floor))
print('Strongest peak : {:.1f} dBFS  @ {:+.1f} kHz from centre  ({:.3f} MHz)'.format(
    peak_db, peak_offset / 1e3, (CENTER + peak_offset) / 1e6))

def band_mean_db(f_lo, f_hi):
    mask = (freqs >= f_lo) & (freqs <= f_hi)
    return float(10 * np.log10(spec[mask].mean() / (BLK * BLK) + 1e-20)) if mask.any() else -999.0

fm_db = band_mean_db(-75e3, 75e3)
ub_db = band_mean_db(_INNER_HZ, _OUTER_HZ)
lb_db = band_mean_db(-_OUTER_HZ, -_INNER_HZ)
dg_db = (ub_db + lb_db) / 2

print('FM carrier (-75..+75 kHz)       : {:.1f} dBFS'.format(fm_db))
print('Upper digital SB ({:.0f}..{:.0f} kHz) : {:.1f} dBFS'.format(
    _INNER_HZ/1e3, _OUTER_HZ/1e3, ub_db))
print('Lower digital SB (-{:.0f}..-{:.0f} kHz): {:.1f} dBFS'.format(
    _OUTER_HZ/1e3, _INNER_HZ/1e3, lb_db))
print('Digital SB SNR (vs noise floor) : {:.1f} dB'.format(dg_db - noise_floor))
print('Digital SB below FM carrier     : {:.1f} dB'.format(fm_db - dg_db))

# ── 3. ASCII spectrum plot ±300 kHz ──────────────────────────────────────────
sep('3. ASCII spectrum  (±300 kHz, 3 kHz/col)')
COL_BW = 3000.0
cols   = int(600e3 / COL_BW)
col_db = np.full(cols, -130.0)
centres = np.linspace(-300e3 + COL_BW/2, 300e3 - COL_BW/2, cols)
for ci, fc in enumerate(centres):
    mask = (freqs >= fc - COL_BW/2) & (freqs < fc + COL_BW/2)
    if mask.any():
        col_db[ci] = 10 * np.log10(spec[mask].mean() / (BLK*BLK) + 1e-20)

lo, hi = -105.0, -30.0
rows = 16
print('  {:.0f} dBFS'.format(hi))
for r in range(rows):
    threshold = hi - (hi - lo) * r / rows
    line = ''
    for ci in range(cols):
        if col_db[ci] >= threshold:
            line += '█'
        elif col_db[ci] >= threshold - (hi - lo) / rows:
            line += '▄'
        else:
            line += ' '
    print('  |{}|'.format(line))
print('  {:.0f} dBFS'.format(lo))
# Frequency axis
axis = ''
for ci, fc in enumerate(centres):
    if abs(fc) < COL_BW/2:
        axis += 'C'
    elif abs(abs(fc) - _INNER_HZ) < COL_BW:
        axis += '|'
    elif abs(abs(fc) - _OUTER_HZ) < COL_BW:
        axis += '|'
    elif ci % 10 == 0:
        axis += '.'
    else:
        axis += ' '
print('  +{}+'.format(axis))
print('  -300kHz  [| = digital SB edges ±{:.0f}/{:.0f} kHz]  +300kHz'.format(
    _INNER_HZ/1e3, _OUTER_HZ/1e3))

# ── 4. Resample ───────────────────────────────────────────────────────────────
sep('4. Resample  {:.0f} kHz → {:.0f} kHz'.format(SR/1e3, _SR/1e3))
need_native = _B * _BLKSZ * _SYM + _CP
raw_need    = int(round(need_native * SR / _SR)) + 1
print('Need {:,} raw samples  ({:.2f} s)'.format(raw_need, raw_need / SR))

chunk = iq[:raw_need]
t0    = time.monotonic()
iq_n  = sp_resample(chunk, need_native).astype(np.complex64)
print('Resample time : {:.2f} s'.format(time.monotonic() - t0))
print('Output power  : {:.1f} dBFS'.format(
    10 * np.log10(float(np.mean(np.abs(iq_n)**2)) + 1e-20)))

# ── 4b. CFO estimation + correction ──────────────────────────────────────────
sep('4b. CFO estimation + correction')
_n_cfo = min(len(iq_n), 32768)
_w_cfo = np.blackman(_n_cfo).astype(np.float32)
_spec_cfo = np.abs(np.fft.fft(iq_n[:_n_cfo] * _w_cfo))**2
_freqs_cfo = np.fft.fftfreq(_n_cfo, 1.0 / _SR)
_mask_cfo  = (np.abs(_freqs_cfo) > 500) & (np.abs(_freqs_cfo) < 75e3)
_cfo_idx   = int(np.argmax(_spec_cfo * _mask_cfo))
cfo_hz     = float(_freqs_cfo[_cfo_idx])
cfo_ppm    = cfo_hz / CENTER * 1e6
print('FM carrier peak : {:+.0f} Hz from DC  ({:+.1f} ppm)'.format(cfo_hz, cfo_ppm))
print('Bin shift       : {:+.1f} subcarriers  ({:.0f} Hz / {:.1f} Hz/SC)'.format(
    cfo_hz / _SC_HZ, cfo_hz, _SC_HZ))

if abs(cfo_hz) > 200:
    _t_cfo = np.arange(len(iq_n), dtype=np.float64) / _SR
    iq_n   = (iq_n * np.exp(-1j * 2.0 * np.pi * cfo_hz * _t_cfo)).astype(np.complex64)
    print('CFO corrected  : {:+.0f} Hz removed'.format(cfo_hz))
else:
    print('CFO < 200 Hz  : no correction needed')

# ── 4c. FM cancellation ───────────────────────────────────────────────────────
# The FM modulation creates sidebands at ±129-198 kHz (the IBOC band).
# Strategy: demodulate FM audio from the clean narrow-band carrier, reconstruct
# the full FM signal (including high-frequency sidebands), subtract.
# Piecewise in 50 ms segments: phase drift per segment ≈ 0.05 rad → ~26 dB cancel.
sep('4c. FM cancellation (audio demod → full FM reconstruct → subtract)')
from scipy.signal import butter, filtfilt as _filtfilt

# 1. FM carrier only: LPF below IBOC inner edge (129 kHz).
_b_fm, _a_fm = butter(6, 90e3 / (_SR / 2), 'low')
_iq_fm = _filtfilt(_b_fm, _a_fm, iq_n.astype(np.complex128)).astype(np.complex64)

# 2. FM amplitude (time-varying, captures AM modulation of the carrier)
_A_fm = np.abs(_iq_fm)

# 3. Instantaneous frequency from LPF'd FM (IBOC excluded → clean estimate)
_pdiff = np.angle(_iq_fm[1:].astype(np.complex128) *
                  np.conj(_iq_fm[:-1].astype(np.complex128)))

# 4. LPF audio to stereo bandwidth (< 55 kHz includes L-R subcarrier at 23-53 kHz)
_b_a, _a_a = butter(4, 55e3 / (_SR / 2), 'low')
_audio = _filtfilt(_b_a, _a_a, _pdiff)

# 5. Reconstruct full FM phase piecewise to limit drift accumulation.
#    Each segment anchors its start phase from the LPF'd FM carrier.
_seg    = int(0.05 * _SR)   # 50 ms ≈ 37 209 samples; drift < 0.05 rad
_phest  = np.empty(len(iq_n), np.float64)
for _i in range(0, len(iq_n), _seg):
    _e  = min(_i + _seg, len(iq_n))
    _p0 = float(np.angle(_iq_fm[_i]))
    _inc = _audio[_i: _e - 1] if _e > _i + 1 else np.empty(0)
    _ph  = np.empty(_e - _i)
    _ph[0] = _p0
    if len(_inc):
        _ph[1:] = _p0 + np.cumsum(_inc)
    _phest[_i: _e] = _ph

# 6. Reconstruct full FM (phase captures all harmonics → includes 129-198 kHz)
_s_fm_est = (_A_fm * np.exp(1j * _phest)).astype(np.complex64)

# 7. Subtract
iq_n_clean = (iq_n - _s_fm_est).astype(np.complex64)

# Report power change in key bands
_nc = min(len(iq_n_clean), 65536)
_sc_c = np.abs(np.fft.fft(iq_n_clean[:_nc])) ** 2
_fr_c = np.fft.fftfreq(_nc, 1.0 / _SR)
_db_c = 10 * np.log10(_sc_c / (_nc * _nc) + 1e-20)
def _bmd(lo, hi):
    m = (_fr_c >= lo) & (_fr_c <= hi)
    return float(10 * np.log10(_sc_c[m].mean() / (_nc * _nc) + 1e-20)) if m.any() else -999
_fm_c  = _bmd(-75e3, 75e3)
_ub_c  = _bmd(_INNER_HZ, _OUTER_HZ)
_lb_c  = _bmd(-_OUTER_HZ, -_INNER_HZ)
_dg_c  = (_ub_c + _lb_c) / 2
_nf_c  = float(np.percentile(_db_c, 10))
print('FM band   before {:.1f} → after {:.1f} dBFS  (Δ {:.1f} dB)'.format(
    fm_db, _fm_c, _fm_c - fm_db))
print('Digital SB before {:.1f} → after {:.1f} dBFS  (Δ {:.1f} dB)'.format(
    dg_db, _dg_c, _dg_c - dg_db))
print('SB SNR vs noise   before {:.1f} → after {:.1f} dB'.format(
    dg_db - noise_floor, _dg_c - _nf_c))
print('FM > digital SB   before {:.1f} → after {:.1f} dB'.format(
    fm_db - dg_db, _fm_c - _dg_c))

iq_n = iq_n_clean   # rest of pipeline uses FM-cancelled signal

# ── 5. Sideband filter + timing ───────────────────────────────────────────────
sep('5. Sideband filter + CP timing (on CFO-corrected, FM-cancelled signal)')
N     = len(iq_n)
scale = N / _FFT
lo_a  = int(_HI_A * scale)
lo_b  = int((_HI_B + 1) * scale)
hi_a  = N - lo_b
hi_b  = N - lo_a
spec2 = np.fft.fft(iq_n)
mask2 = np.zeros(N, dtype=bool)
mask2[lo_a:lo_b] = True
mask2[hi_a:hi_b] = True
spec2[~mask2]    = 0.0
iq_f  = np.fft.ifft(spec2).astype(np.complex64)
print('Filter kept {}/{} bins ({:.2f}%)'.format(mask2.sum(), N, 100*mask2.sum()/N))
print('Filtered power : {:.1f} dBFS'.format(
    10 * np.log10(float(np.mean(np.abs(iq_f)**2)) + 1e-20)))

n    = len(iq_f) - _FFT - _CP
prod = iq_f[:n] * np.conj(iq_f[_FFT: _FFT + n])
cs   = np.concatenate([[0j], np.cumsum(prod)])
metric = np.abs(cs[_CP: n + 1] - cs[: n + 1 - _CP]).astype(np.float32)
mean   = float(metric.mean()) + 1e-9

# Search 4 symbols for the best offset
search = min(_SYM * 4, len(metric))
off    = int(np.argmax(metric[:search]))
q1     = float(metric[off]) / mean
q2     = float(metric[min(off + _SYM, len(metric)-1)]) / mean
sync_q = (q1 + q2) / 2.0

print('CP peak offset : {} samples'.format(off))
print('q1={:.2f}  q2={:.2f}  sync_q={:.2f}  (threshold 6.0)'.format(q1, q2, sync_q))

# How does the metric look across the first 4 symbols?
print('\nCP metric profile (4-symbol window, every 108 samples):')
for i in range(0, min(_SYM*4, len(metric)), 108):
    bar = int(metric[i] / mean * 10)
    print('  {:5d}  q={:.2f}  {}'.format(i, metric[i]/mean, '█'*min(bar,40)))

# ── 6. OFDM FFT ───────────────────────────────────────────────────────────────
avail_full = (len(iq_n) - off) // _SYM
avail      = min(avail_full, _B * _BLKSZ)   # cap at 512 for interleaver
sep('6. OFDM FFT  ({} symbols from offset {})'.format(avail, off))
syms  = iq_n[off: off + avail * _SYM].reshape(avail, _SYM)[:, _CP:]
ffts  = np.fft.fft(syms, axis=1).astype(np.complex64)

for label, bins in [('Upper SB', _UB_DATA_BINS), ('Lower SB', _LB_DATA_BINS)]:
    sc    = ffts[:, bins]
    # Two-pass: per-symbol CPE then per-subcarrier static phase
    cpe   = np.angle(np.mean(sc**2, axis=1, keepdims=True)) / 2.0
    sc1   = sc * np.exp(-1j * cpe)
    theta = np.angle(np.mean(sc1**2, axis=0)) / 2.0
    sc_c  = sc1 * np.exp(-1j * theta)
    sig   = float(np.mean(sc_c.real**2))
    nse   = float(np.mean(sc_c.imag**2)) + 1e-20
    mer   = 10 * np.log10(sig / nse)
    # EVM
    sc_n  = sc_c / (np.std(sc_c.real) + 1e-9)
    evm   = float(np.std(np.abs(sc_n.real) - 1.0))
    print('{}: MER={:+.1f} dB  EVM={:.3f}  sig/nse={:.3f}/{:.3f}'.format(
        label, mer, evm, sig, nse))

# ── 7. Check if any subcarriers look like BPSK/QPSK ──────────────────────────
sep('7. Per-subcarrier SNR  (upper SB, first 4 partitions)')
for p in range(4):
    sc_vals = ffts[:, _UB_DATA_BINS[p*18:(p+1)*18]]  # (avail, 18)
    theta   = np.angle(np.mean(sc_vals**2, axis=0)) / 2.0
    sc_c    = sc_vals * np.exp(-1j * theta)
    snrs    = 10 * np.log10(
        np.mean(sc_c.real**2, axis=0) /
        (np.mean(sc_c.imag**2, axis=0) + 1e-20))
    print('  Partition {:d}: SNR min={:+.1f} max={:+.1f} mean={:+.1f} dB'.format(
        p, float(snrs.min()), float(snrs.max()), float(snrs.mean())))

# ── 8. Summary ────────────────────────────────────────────────────────────────
sep('Summary')
print('Digital SB SNR vs noise floor : {:.1f} dB'.format(dg_db - noise_floor))
print('Digital SB below FM carrier   : {:.1f} dB'.format(fm_db - dg_db))
print('CP sync_q                     : {:.2f}  (need > 6.0)'.format(sync_q))
sc = ffts[:, _UB_DATA_BINS]
theta = np.angle(np.mean(sc**2, axis=0)) / 2.0
sc_c  = sc * np.exp(-1j * theta)
mer_u = 10 * np.log10(float(np.mean(sc_c.real**2)) / (float(np.mean(sc_c.imag**2)) + 1e-20))
print('MER (upper SB)                : {:+.1f} dB  (need > ~10 dB)'.format(mer_u))
print()
if dg_db - noise_floor < 5:
    print('LIKELY CAUSE: Station does not broadcast HD Radio (IBOC),')
    print('  or digital sidebands are below the receiver noise floor.')
elif sync_q < 2.0:
    print('LIKELY CAUSE: Signal present but sync too weak (sync_q < 2.0).')
else:
    print('Signal and sync look usable — running full pipeline below.')

# ── 9. Full pipeline: buffer_pm → deinterleave → Viterbi → HDLC ──────────────
sep('9. Full Viterbi pipeline')
n_sym_use = (avail // _BLKSZ) * _BLKSZ   # must be multiple of 32
if n_sym_use < _BLKSZ:
    print('Not enough symbols ({}) for a full block.'.format(avail))
    sys.exit(0)

lb = ffts[:n_sym_use, _LB_BINS]   # (n_sym, 180)
ub = ffts[:n_sym_use, _UB_BINS]

def _correct(sc):
    # Pass 1: per-symbol CPE (corrects residual CFO & slow phase drift).
    # Average sc² over all subcarriers → angle / 2 = common phase per symbol.
    cpe   = np.angle(np.mean(sc ** 2, axis=1, keepdims=True)) / 2.0  # (n,1)
    sc_c1 = sc * np.exp(-1j * cpe)
    # Pass 2: per-subcarrier static phase (corrects channel phase offset).
    theta = np.angle(np.mean(sc_c1 ** 2, axis=0)) / 2.0              # (180,)
    return (sc_c1 * np.exp(-1j * theta)).astype(np.complex64)

lb_c = _correct(lb)
ub_c = _correct(ub)

lb_iq = np.stack([lb_c.reshape(n_sym_use, _PM_PARTS, _PART_DATA).real,
                  lb_c.reshape(n_sym_use, _PM_PARTS, _PART_DATA).imag],
                 axis=-1).reshape(n_sym_use, _PM_PARTS, _C)
ub_iq = np.stack([ub_c.reshape(n_sym_use, _PM_PARTS, _PART_DATA).real,
                  ub_c.reshape(n_sym_use, _PM_PARTS, _PART_DATA).imag],
                 axis=-1).reshape(n_sym_use, _PM_PARTS, _C)
buf_pm = np.concatenate([lb_iq, ub_iq], axis=1).reshape(n_sym_use, _J * _C).astype(np.float32)

print('n_sym_use={} ({}×{} frames)'.format(n_sym_use, n_sym_use//_BLKSZ, _BLKSZ))
n_blocks = n_sym_use // _BLKSZ   # should be ≤ _B=16

crc_hits = 0
for bc_off in range(_B):
    frames  = buf_pm[:n_sym_use].reshape(n_sym_use // _BLKSZ, _BLKSZ, _J * _C)
    # Pad with zeros if we have fewer than _B blocks
    if len(frames) < _B:
        pad = np.zeros((_B - len(frames), _BLKSZ, _J * _C), dtype=np.float32)
        frames = np.concatenate([frames, pad], axis=0)
    ordered            = np.empty((_B, _BLKSZ, _J * _C), dtype=np.float32)
    bc_idxs            = (np.arange(_B) + bc_off) % _B
    ordered[bc_idxs]   = frames
    buf_flat           = ordered.reshape(_PM_BUF)
    vit_in             = _deinterleave_p1(buf_flat)
    t0                 = time.monotonic()
    bits               = _viterbi_r3(vit_in)
    dt                 = time.monotonic() - t0
    bits_d             = _descramble_bits(bits)
    raw_bytes          = _frame_pack(bits_d)
    pdus               = _scan_hdlc_frames(raw_bytes)
    found = list(pdus.keys())
    print('  bc_off={:2d}  vit {:.1f}s  PDUs={}'.format(bc_off, dt, found if found else '—'))
    if found:
        crc_hits += 1
        for k, v in pdus.items():
            print('    {} = {!r}'.format(k, v))

sep('Result')
print('CRC hits across all bc_offsets: {}/{}'.format(crc_hits, _B))
