"""
Train a small IQ modulation classifier and export it to ONNX.

Generates synthetic IQ training data for 8 common modulation types, trains
a 1-D ResNet, then writes models/modclass_lite.onnx (~150 k parameters,
~600 KB on disk).  No GPU required; typical runtime < 5 min on CPU.

Usage:
    uv run --with torch --with onnxscript scripts/train_modclass.py

The onnxruntime package (needed only at inference time) is separate — install
it with:
    uv add --group ml onnxruntime
"""

import os
import sys
import time
import numpy as np
from scipy.signal import butter, lfilter, decimate

# ── constants ──────────────────────────────────────────────────────────────────

SR      = 200_000   # model sample rate (Hz)
N       = 1_024     # samples per example
N_TRAIN = 2_000     # examples per class (training)
N_VAL   = 400       # examples per class (validation)
BATCH   = 64
EPOCHS  = 40
LR      = 1e-3

CLASSES = ['OOK', 'AM-DSB', 'WBFM', 'BPSK', 'QPSK', '8PSK', 'QAM16', 'FSK']
N_CLS   = len(CLASSES)

OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'models')
OUT_MODEL = os.path.join(OUT_DIR, 'modclass_lite.onnx')


# ── signal generators ──────────────────────────────────────────────────────────

def _lpf(sig, cutoff_hz, order=4):
    b, a = butter(order, cutoff_hz / (SR / 2), btype='low')
    return lfilter(b, a, sig)


def _awgn(sig, snr_db):
    snr    = 10 ** (snr_db / 10)
    pwr    = np.mean(np.abs(sig) ** 2) + 1e-20
    sigma  = np.sqrt(pwr / (2 * snr))
    noise  = sigma * (np.random.randn(len(sig)) + 1j * np.random.randn(len(sig)))
    return (sig + noise).astype(np.complex64)


def _freq_shift(sig, offset_hz):
    t = np.arange(len(sig)) / SR
    return sig * np.exp(2j * np.pi * offset_hz * t)


def gen_ook(n):
    baud  = np.random.randint(4_000, 20_000)
    sps   = max(1, SR // baud)
    bits  = np.random.randint(0, 2, n // sps + 2)
    syms  = np.repeat(bits.astype(np.float32), sps)[:n]
    return syms.astype(np.complex64)


def gen_am_dsb(n):
    carrier = np.random.uniform(10_000, 40_000)
    mod_idx = np.random.uniform(0.5, 1.0)
    msg     = _lpf(np.random.randn(n), 8_000).astype(np.float32)
    msg    /= np.max(np.abs(msg)) + 1e-8
    t       = np.arange(n) / SR
    return ((1 + mod_idx * msg) * np.exp(2j * np.pi * carrier * t)).astype(np.complex64)


def gen_wbfm(n):
    deviation = np.random.uniform(15_000, 40_000)
    msg       = _lpf(np.random.randn(n), 12_000).astype(np.float32)
    msg      /= np.max(np.abs(msg)) + 1e-8
    phase     = 2 * np.pi * deviation / SR * np.cumsum(msg)
    return np.exp(1j * phase).astype(np.complex64)


def _pulse_shape(syms_complex, sps):
    """Upsample then apply a simple rectangular pulse (no RRC for brevity)."""
    up = np.repeat(syms_complex, sps)
    return up


def gen_bpsk(n):
    baud = np.random.randint(10_000, 40_000)
    sps  = max(1, SR // baud)
    bits = np.random.choice(np.array([-1, 1], dtype=np.float32), n // sps + 2)
    return _pulse_shape(bits.astype(np.complex64), sps)[:n]


def gen_qpsk(n):
    baud    = np.random.randint(10_000, 30_000)
    sps     = max(1, SR // baud)
    choices = np.array([1+1j, -1+1j, -1-1j, 1-1j], dtype=np.complex64) / np.sqrt(2)
    syms    = np.random.choice(choices, n // sps + 2)
    return _pulse_shape(syms, sps)[:n]


def gen_8psk(n):
    baud    = np.random.randint(8_000, 25_000)
    sps     = max(1, SR // baud)
    angles  = np.random.randint(0, 8, n // sps + 2) * (2 * np.pi / 8)
    syms    = np.exp(1j * angles).astype(np.complex64)
    return _pulse_shape(syms, sps)[:n]


def gen_qam16(n):
    baud  = np.random.randint(5_000, 15_000)
    sps   = max(1, SR // baud)
    pts   = np.array([-3, -1, 1, 3], dtype=np.float32)
    re    = np.random.choice(pts, n // sps + 2)
    im    = np.random.choice(pts, n // sps + 2)
    syms  = (re + 1j * im).astype(np.complex64) / (3 * np.sqrt(2))
    return _pulse_shape(syms, sps)[:n]


def gen_fsk(n):
    baud      = np.random.randint(5_000, 20_000)
    sps       = max(1, SR // baud)
    deviation = np.random.uniform(5_000, 20_000)
    bits      = np.random.randint(0, 2, n // sps + 2)
    freqs     = np.where(bits == 0, -deviation, deviation)
    freq_seq  = np.repeat(freqs, sps)[:n]
    phase     = 2 * np.pi / SR * np.cumsum(freq_seq)
    return np.exp(1j * phase).astype(np.complex64)


_GENS = [gen_ook, gen_am_dsb, gen_wbfm, gen_bpsk,
         gen_qpsk, gen_8psk, gen_qam16, gen_fsk]


def _make_example(gen_fn, snr_db):
    sig = gen_fn(N + 64)                              # extra samples for filter transient
    # Random freq offset ± 15 % of SR/2
    offset = np.random.uniform(-0.15, 0.15) * SR / 2
    sig    = _freq_shift(sig, offset)
    sig    = _awgn(sig, snr_db)
    sig    = sig[64:64 + N]                            # drop transient
    # Normalise to zero-mean unit-variance per channel
    sig   -= sig.mean()
    std    = np.std(np.abs(sig)) + 1e-8
    sig   /= std
    return np.stack([sig.real, sig.imag]).astype(np.float32)  # (2, N)


def build_dataset(n_per_class, snr_range=(-5, 25)):
    X, y = [], []
    for cls_idx, gen in enumerate(_GENS):
        for _ in range(n_per_class):
            snr = np.random.uniform(*snr_range)
            X.append(_make_example(gen, snr))
            y.append(cls_idx)
    X = np.array(X, dtype=np.float32)
    y = np.array(y, dtype=np.int64)
    perm = np.random.permutation(len(y))
    return X[perm], y[perm]


# ── model ──────────────────────────────────────────────────────────────────────

def build_model():
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
                nn.BatchNorm1d(32),
                nn.ReLU(),
                nn.MaxPool1d(4),            # → (32, 256)
            )
            self.body = nn.Sequential(
                ResBlock(32, 5),
                nn.Conv1d(32, 64, 1, bias=False),
                nn.BatchNorm1d(64),
                nn.ReLU(),
                nn.MaxPool1d(4),            # → (64, 64)
                ResBlock(64, 3),
                nn.Conv1d(64, 128, 1, bias=False),
                nn.BatchNorm1d(128),
                nn.ReLU(),
                nn.MaxPool1d(4),            # → (128, 16)
                ResBlock(128, 3),
            )
            self.head = nn.Linear(128, n_classes)

        def forward(self, x):
            x = self.stem(x)
            x = self.body(x)
            x = x.mean(dim=-1)             # global average pool
            return self.head(x)

    return ModClassNet(N_CLS)


# ── training loop ──────────────────────────────────────────────────────────────

def train():
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        print('PyTorch not found.  Install with:')
        print('    uv run --with torch scripts/train_modclass.py')
        sys.exit(1)

    print('Building training set …')
    t0   = time.monotonic()
    Xtr, ytr = build_dataset(N_TRAIN)
    Xva, yva = build_dataset(N_VAL, snr_range=(0, 20))
    print(f'  {len(ytr)} train / {len(yva)} val  ({time.monotonic()-t0:.1f}s)')

    tr_ds = TensorDataset(torch.from_numpy(Xtr), torch.from_numpy(ytr))
    va_ds = TensorDataset(torch.from_numpy(Xva), torch.from_numpy(yva))
    tr_dl = DataLoader(tr_ds, batch_size=BATCH, shuffle=True,  num_workers=0)
    va_dl = DataLoader(va_ds, batch_size=BATCH, shuffle=False, num_workers=0)

    device = 'mps' if torch.backends.mps.is_available() else \
             'cuda' if torch.cuda.is_available() else 'cpu'
    print(f'Device: {device}')

    model = build_model().to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f'Parameters: {n_params:,}')

    opt   = torch.optim.Adam(model.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    crit  = nn.CrossEntropyLoss()

    best_acc = 0.0
    best_sd  = None

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
        sched.step()

        model.eval()
        correct = total = 0
        with torch.no_grad():
            for xb, yb in va_dl:
                xb, yb = xb.to(device), yb.to(device)
                preds   = model(xb).argmax(dim=1)
                correct += (preds == yb).sum().item()
                total   += len(yb)
        acc = correct / total

        lr_now = sched.get_last_lr()[0]
        print(f'  Epoch {epoch:3d}/{EPOCHS}  loss={tr_loss/len(ytr):.4f}'
              f'  val_acc={acc:.3f}  lr={lr_now:.2e}')

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
        input_names=['input'],
        output_names=['logits'],
        dynamic_axes={'input': {0: 'batch'}, 'logits': {0: 'batch'}},
        opset_version=17,
    )

    # The new torch exporter may write external data files.  Re-save as a
    # single self-contained .onnx so onnxruntime can load it from any cwd.
    try:
        import onnx
        proto = onnx.load(OUT_MODEL)                   # inlines external data
        data_file = OUT_MODEL + '.data'
        if os.path.exists(data_file):
            os.remove(data_file)
        onnx.save(proto, OUT_MODEL)                    # single-file save
    except ImportError:
        pass   # onnxscript brings onnx along, but guard anyway

    size_kb = os.path.getsize(OUT_MODEL) / 1024
    print(f'Exported → {OUT_MODEL}  ({size_kb:.0f} KB)')
    print(f'Classes:  {CLASSES}')


if __name__ == '__main__':
    np.random.seed(42)
    train()
