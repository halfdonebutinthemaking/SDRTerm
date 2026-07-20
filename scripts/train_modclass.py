"""
Train the modulation classifier and export to ONNX.

Two data modes:
  --data PATH   Use RadioML 2018.01a HDF5 file (recommended, ~3.5 GB).
                Download with:  uv run scripts/download_radioml.py
  (no --data)   Generate synthetic IQ data (fast, lower real-world accuracy).

Usage:
    uv run --with torch --with onnxscript scripts/train_modclass.py
    uv run --with torch --with onnxscript --with h5py scripts/train_modclass.py \\
        --data data/GOLD_XYZ_OSC.0001_1024.hdf5

Throttling (if training freezes the laptop):
    --device cpu      avoid saturating the GPU/MPS
    --threads 2       limit CPU thread usage (default: all cores)
    --batch 32        smaller batches, less memory pressure
    --throttle 50     sleep 50 ms between batches

The onnxruntime package (inference only) is installed separately:
    uv add --group ml onnxruntime
"""

import argparse
import json
import os
import sys
import time
import numpy as np
from scipy.signal import butter, lfilter

# ── constants ──────────────────────────────────────────────────────────────────

SR      = 200_000   # model sample rate (Hz)
N       = 1_024     # samples per example
BATCH   = 64
EPOCHS  = 40
LR      = 1e-3

OUT_DIR    = os.path.join(os.path.dirname(__file__), '..', 'models')
OUT_MODEL  = os.path.join(OUT_DIR, 'modclass_lite.onnx')
OUT_LABELS = os.path.join(OUT_DIR, 'modclass_labels.json')

# Both filename variants that exist in the wild for RadioML 2018.01a
_RADIOML_FNAMES = [
    'GOLD_XYZ_OSC.0001_1024.hdf5',   # Kaggle release
    'GOLD_XYZ_OSC.0_1024.hdf5',       # original DeepSig release
]

# RadioML 2018.01a class ordering (24 classes).
# This ordering matches the one-hot Y matrix in the canonical HDF5 file.
# Verified against DeepSig's original dataset release notes.
_RADIOML_CLASSES = [
    '32PSK', '16APSK', '32QAM', 'FM', 'GMSK', '32APSK',
    'OFDM-256', 'OFDM-512', 'OFDM-128', '256QAM', 'OFDM-2048',
    'AM-SSB-SC', 'AM-DSB-WC', 'OFDM-1024', 'BPSK', 'QPSK', '8PSK',
    'AM-DSB-SC', 'AM-SSB-WC', '64QAM', 'OFDM-64', '16PSK', '16QAM', 'OFDM-72',
]

# Synthetic-only classes (fallback when no HDF5 provided)
_SYNTH_CLASSES = ['OOK', 'AM-DSB', 'WBFM', 'BPSK', 'QPSK', '8PSK', 'QAM16', 'FSK']


# ── RadioML loader ─────────────────────────────────────────────────────────────

def load_radioml(path: str, snr_min: int = 0,
                 max_per_class: int = 6_000):
    """
    Load RadioML 2018.01a from HDF5.  Returns (X, y, classes).

    X: float32 (N, 2, 1024)
    y: int64   (N,)
    classes: list of str, len = number of classes

    snr_min:       discard examples below this SNR (dB).  0 dB balances
                   accuracy vs. noise robustness in training.
    max_per_class: subsample to this many examples per class so the full
                   3.5 GB dataset fits in RAM during training.
    """
    try:
        import h5py
    except ImportError:
        print('h5py not found.  Run:')
        print('  uv run --with torch --with onnxscript --with h5py '
              'scripts/train_modclass.py --data <path>')
        sys.exit(1)

    print(f'Loading {path} …')
    t0 = time.monotonic()

    with h5py.File(path, 'r') as f:
        keys = list(f.keys())
        print(f'  HDF5 keys: {keys}')

        X_all = f['X'][:]          # expected (N, 2, 1024)
        Y_all = f['Y'][:]          # expected (N, n_classes) one-hot
        Z_all = f['Z'][:]          # expected (N,) SNR values

        print(f'  X {X_all.shape}  Y {Y_all.shape}  Z {Z_all.shape}')

        # Some variants (Kaggle) store Z as (N, 1) — squeeze to 1D
        Z_all = Z_all.ravel()

        # Some variants store X as (N, 1024, 2) channel-last — transpose to (N, 2, 1024)
        if X_all.ndim == 3 and X_all.shape[-1] == 2 and X_all.shape[1] != 2:
            print(f'  Transposing X from channel-last {X_all.shape} to channel-first')
            X_all = X_all.transpose(0, 2, 1)

        # Try to read class names from file metadata; fall back to known list
        classes = None
        for attr in ('mod_classes', 'class_labels', 'mods'):
            val = f.attrs.get(attr)
            if val is not None:
                classes = [s.decode() if isinstance(s, bytes) else str(s)
                           for s in val]
                print(f'  Classes from HDF5 attr "{attr}": {classes}')
                break
        for dset in ('mod_classes', 'class_labels', 'mods'):
            if dset in f and classes is None:
                raw = f[dset][:]
                classes = [s.decode() if isinstance(s, bytes) else str(s)
                           for s in raw]
                print(f'  Classes from HDF5 dataset "{dset}": {classes}')
                break

    n_classes = Y_all.shape[1]

    if classes is None:
        if n_classes == len(_RADIOML_CLASSES):
            classes = _RADIOML_CLASSES
            print(f'  Using built-in RadioML 2018.01a class list '
                  f'({n_classes} classes)')
        else:
            classes = [f'class_{i}' for i in range(n_classes)]
            print(f'  WARNING: {n_classes} classes in file but '
                  f'{len(_RADIOML_CLASSES)} in built-in list. '
                  f'Using generic names.')
    elif len(classes) != n_classes:
        print(f'  WARNING: class list length {len(classes)} != '
              f'Y width {n_classes}. Truncating/padding.')
        classes = (classes + [f'class_{i}' for i in range(n_classes)])[:n_classes]

    y_all = np.argmax(Y_all, axis=1).astype(np.int64)

    # Filter by SNR
    snr_mask = Z_all >= snr_min
    X_all, y_all, Z_all = X_all[snr_mask], y_all[snr_mask], Z_all[snr_mask]
    print(f'  {snr_mask.sum():,} examples with SNR ≥ {snr_min} dB '
          f'(of {len(snr_mask):,} total)')

    # Subsample per class for memory
    idx = []
    for c in range(n_classes):
        c_idx = np.where(y_all == c)[0]
        if len(c_idx) > max_per_class:
            c_idx = np.random.choice(c_idx, max_per_class, replace=False)
        idx.append(c_idx)
    idx = np.concatenate(idx)
    np.random.shuffle(idx)
    X_all, y_all = X_all[idx], y_all[idx]
    print(f'  Subsampled to {len(y_all):,} examples  '
          f'({time.monotonic() - t0:.1f}s)')

    # Split 85/15 train/val
    split = int(0.85 * len(y_all))
    return (X_all[:split].astype(np.float32), y_all[:split],
            X_all[split:].astype(np.float32), y_all[split:],
            classes)


# ── synthetic data ─────────────────────────────────────────────────────────────

def _lpf(sig, cutoff_hz, order=4):
    b, a = butter(order, cutoff_hz / (SR / 2), btype='low')
    return lfilter(b, a, sig)

def _awgn(sig, snr_db):
    snr   = 10 ** (snr_db / 10)
    pwr   = np.mean(np.abs(sig) ** 2) + 1e-20
    sigma = np.sqrt(pwr / (2 * snr))
    noise = sigma * (np.random.randn(len(sig)) + 1j * np.random.randn(len(sig)))
    return (sig + noise).astype(np.complex64)

def _freq_shift(sig, offset_hz):
    t = np.arange(len(sig)) / SR
    return sig * np.exp(2j * np.pi * offset_hz * t)

def _gen_ook(n):
    baud = np.random.randint(4_000, 20_000)
    sps  = max(1, SR // baud)
    bits = np.random.randint(0, 2, n // sps + 2)
    return np.repeat(bits.astype(np.float32), sps)[:n].astype(np.complex64)

def _gen_am_dsb(n):
    carrier = np.random.uniform(10_000, 40_000)
    mod_idx = np.random.uniform(0.5, 1.0)
    msg     = _lpf(np.random.randn(n), 8_000).astype(np.float32)
    msg    /= np.max(np.abs(msg)) + 1e-8
    t       = np.arange(n) / SR
    return ((1 + mod_idx * msg) * np.exp(2j * np.pi * carrier * t)).astype(np.complex64)

def _gen_wbfm(n):
    deviation = np.random.uniform(15_000, 40_000)
    msg       = _lpf(np.random.randn(n), 12_000).astype(np.float32)
    msg      /= np.max(np.abs(msg)) + 1e-8
    return np.exp(1j * 2 * np.pi * deviation / SR * np.cumsum(msg)).astype(np.complex64)

def _gen_psk(n, m):
    baud    = np.random.randint(10_000, 30_000)
    sps     = max(1, SR // baud)
    angles  = np.random.randint(0, m, n // sps + 2) * (2 * np.pi / m)
    syms    = np.exp(1j * angles).astype(np.complex64)
    return np.repeat(syms, sps)[:n]

def _gen_qam16(n):
    baud  = np.random.randint(5_000, 15_000)
    sps   = max(1, SR // baud)
    pts   = np.array([-3, -1, 1, 3], dtype=np.float32)
    re    = np.random.choice(pts, n // sps + 2)
    im    = np.random.choice(pts, n // sps + 2)
    syms  = (re + 1j * im).astype(np.complex64) / (3 * np.sqrt(2))
    return np.repeat(syms, sps)[:n]

def _gen_fsk(n):
    baud      = np.random.randint(5_000, 20_000)
    sps       = max(1, SR // baud)
    deviation = np.random.uniform(5_000, 20_000)
    bits      = np.random.randint(0, 2, n // sps + 2)
    freqs     = np.where(bits == 0, -deviation, deviation)
    phase     = 2 * np.pi / SR * np.cumsum(np.repeat(freqs, sps)[:n])
    return np.exp(1j * phase).astype(np.complex64)

_SYNTH_GENS = [_gen_ook, _gen_am_dsb, _gen_wbfm,
               lambda n: _gen_psk(n, 2),   # BPSK
               lambda n: _gen_psk(n, 4),   # QPSK
               lambda n: _gen_psk(n, 8),   # 8PSK
               _gen_qam16, _gen_fsk]

def _make_synth_example(gen_fn, snr_db):
    sig  = gen_fn(N + 64)
    sig  = _freq_shift(sig, np.random.uniform(-0.15, 0.15) * SR / 2)
    sig  = _awgn(sig, snr_db)
    sig  = sig[64: 64 + N]
    sig -= sig.mean()
    sig /= np.std(np.abs(sig)) + 1e-8
    return np.stack([sig.real, sig.imag]).astype(np.float32)

def build_synthetic(n_per_class, snr_range=(-5, 25)):
    X, y = [], []
    for cls_idx, gen in enumerate(_SYNTH_GENS):
        for _ in range(n_per_class):
            snr = np.random.uniform(*snr_range)
            X.append(_make_synth_example(gen, snr))
            y.append(cls_idx)
    X = np.array(X, dtype=np.float32)
    y = np.array(y, dtype=np.int64)
    perm = np.random.permutation(len(y))
    return X[perm], y[perm]


# ── model ──────────────────────────────────────────────────────────────────────

def build_model(n_classes: int):
    import torch.nn as nn

    class ResBlock(nn.Module):
        def __init__(self, ch, k):
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv1d(ch, ch, k, padding=k // 2, bias=False),
                nn.BatchNorm1d(ch),
                nn.ReLU(),
                nn.Conv1d(ch, ch, k, padding=k // 2, bias=False),
                nn.BatchNorm1d(ch),
            )
            self.act = nn.ReLU()
        def forward(self, x):
            return self.act(x + self.net(x))

    class ModClassNet(nn.Module):
        def __init__(self, n_classes):
            super().__init__()
            self.stem = nn.Sequential(
                nn.Conv1d(2, 32, 7, padding=3, bias=False),
                nn.BatchNorm1d(32), nn.ReLU(), nn.MaxPool1d(4),
            )
            self.body = nn.Sequential(
                ResBlock(32, 5),
                nn.Conv1d(32, 64, 1, bias=False), nn.BatchNorm1d(64), nn.ReLU(),
                nn.MaxPool1d(4),
                ResBlock(64, 3),
                nn.Conv1d(64, 128, 1, bias=False), nn.BatchNorm1d(128), nn.ReLU(),
                nn.MaxPool1d(4),
                ResBlock(128, 3),
            )
            self.head = nn.Linear(128, n_classes)
        def forward(self, x):
            return self.head(self.body(self.stem(x)).mean(dim=-1))

    return ModClassNet(n_classes)


# ── training loop ──────────────────────────────────────────────────────────────

def train(args):
    import time as _time
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        print('PyTorch not found.  Run:')
        print('  uv run --with torch --with onnxscript scripts/train_modclass.py')
        sys.exit(1)

    # ── thread / device setup ──────────────────────────────────────────────────
    if args.threads:
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(min(args.threads, 2))
        print(f'CPU threads limited to {args.threads}')

    if args.device == 'auto':
        device = ('mps'  if torch.backends.mps.is_available() else
                  'cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = args.device

    throttle_s = (args.throttle / 1000.0) if args.throttle else 0.0

    # ── data ───────────────────────────────────────────────────────────────────
    if args.data:
        Xtr, ytr, Xva, yva, classes = load_radioml(
            args.data,
            snr_min=args.snr_min,
            max_per_class=args.max_per_class,
        )
        print(f'RadioML: {len(ytr):,} train / {len(yva):,} val  '
              f'({len(classes)} classes)')
    else:
        print('No --data provided — generating synthetic training set …')
        t0 = _time.monotonic()
        Xtr, ytr = build_synthetic(2_000)
        Xva, yva = build_synthetic(400, snr_range=(0, 20))
        classes  = _SYNTH_CLASSES
        print(f'  {len(ytr):,} train / {len(yva):,} val  '
              f'({_time.monotonic()-t0:.1f}s)')

    n_classes  = len(classes)
    batch_size = args.batch

    tr_ds = TensorDataset(torch.from_numpy(Xtr), torch.from_numpy(ytr))
    va_ds = TensorDataset(torch.from_numpy(Xva), torch.from_numpy(yva))
    tr_dl = DataLoader(tr_ds, batch_size=batch_size, shuffle=True,  num_workers=0)
    va_dl = DataLoader(va_ds, batch_size=batch_size, shuffle=False, num_workers=0)

    print(f'Device: {device}   Batch: {batch_size}   '
          f'Throttle: {args.throttle} ms   Classes: {n_classes}')

    model = build_model(n_classes).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f'Parameters: {n_params:,}')

    opt   = torch.optim.Adam(model.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    crit  = nn.CrossEntropyLoss()

    best_acc, best_sd = 0.0, None
    for epoch in range(1, EPOCHS + 1):
        model.train()
        tr_loss = 0.0
        for xb, yb in tr_dl:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = crit(model(xb), yb)
            loss.backward()
            opt.step()
            tr_loss += loss.item() * len(yb)
            if throttle_s:
                _time.sleep(throttle_s)
        sched.step()

        model.eval()
        correct = total = 0
        with torch.no_grad():
            for xb, yb in va_dl:
                xb, yb = xb.to(device), yb.to(device)
                correct += (model(xb).argmax(1) == yb).sum().item()
                total   += len(yb)
        acc = correct / total

        print(f'  Epoch {epoch:3d}/{EPOCHS}  '
              f'loss={tr_loss/len(Xtr):.4f}  '
              f'val_acc={acc:.3f}  '
              f'lr={sched.get_last_lr()[0]:.2e}')

        if acc > best_acc:
            best_acc = acc
            best_sd  = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    print(f'\nBest val accuracy: {best_acc:.3f}')

    # ── export ─────────────────────────────────────────────────────────────────
    model.load_state_dict(best_sd)
    model.eval().cpu()
    dummy = torch.zeros(1, 2, N)
    os.makedirs(OUT_DIR, exist_ok=True)

    torch.onnx.export(
        model, dummy, OUT_MODEL,
        input_names=['input'], output_names=['logits'],
        dynamic_axes={'input': {0: 'batch'}, 'logits': {0: 'batch'}},
        opset_version=17,
    )

    # Inline any external data so the model is a single self-contained file
    try:
        import onnx
        proto    = onnx.load(OUT_MODEL)
        data_file = OUT_MODEL + '.data'
        if os.path.exists(data_file):
            os.remove(data_file)
        onnx.save(proto, OUT_MODEL)
    except ImportError:
        pass

    # Write class labels alongside the model
    with open(OUT_LABELS, 'w') as f:
        json.dump(classes, f, indent=2)

    size_kb = os.path.getsize(OUT_MODEL) / 1024
    print(f'Exported  → {OUT_MODEL}  ({size_kb:.0f} KB)')
    print(f'Labels    → {OUT_LABELS}')
    print(f'Classes   : {classes}')


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--data', metavar='PATH',
                        help='Path to GOLD_XYZ_OSC.0001_1024.hdf5 (RadioML 2018.01a)')
    parser.add_argument('--snr-min', type=int, default=0,
                        help='Minimum SNR (dB) to include from RadioML (default: 0)')
    parser.add_argument('--max-per-class', type=int, default=6_000,
                        help='Max examples per class from RadioML (default: 6000)')
    parser.add_argument('--device', default='auto',
                        choices=['auto', 'cpu', 'mps', 'cuda'],
                        help='Training device (default: auto). Use cpu to avoid '
                             'saturating the GPU and freezing the system.')
    parser.add_argument('--threads', type=int, default=None,
                        help='Limit PyTorch CPU threads (e.g. 2). '
                             'Default: use all available cores.')
    parser.add_argument('--batch', type=int, default=BATCH,
                        help=f'Batch size (default: {BATCH}). '
                             'Smaller values reduce memory pressure.')
    parser.add_argument('--throttle', type=int, default=0, metavar='MS',
                        help='Sleep this many milliseconds between batches '
                             '(default: 0). Keeps the system responsive.')
    args = parser.parse_args()

    np.random.seed(42)
    train(args)
