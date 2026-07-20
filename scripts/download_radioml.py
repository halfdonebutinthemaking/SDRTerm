"""
Download the RadioML 2018.01a dataset.

Preferred source (Kaggle, ~3.5 GB, free account required):
    https://www.kaggle.com/datasets/pinxau1000/radioml2018

Quick start:
    pip install kaggle
    # put your kaggle.json token at ~/.kaggle/kaggle.json
    uv run scripts/download_radioml.py

Or download the HDF5 file manually from the Kaggle page and place it in
the data/ directory, then run:
    uv run --with torch --with onnxscript scripts/train_modclass.py \\
        --data data/GOLD_XYZ_OSC.0001_1024.hdf5
"""

import argparse
import os
import sys
import tarfile
import urllib.request
import urllib.error
import subprocess

_KAGGLE_DATASET = 'pinxau1000/radioml2018'

# Both filename variants seen in the wild
_FNAMES = [
    'GOLD_XYZ_OSC.0001_1024.hdf5',   # Kaggle version
    'GOLD_XYZ_OSC.0_1024.hdf5',       # original DeepSig release
]

_DEFAULT_OUT = os.path.join(os.path.dirname(__file__), '..', 'data')

# Fallback direct URLs (DeepSig opendata — may be unavailable)
_DIRECT_URLS = [
    'https://opendata.deepsig.io/datasets/2018.01/GOLD_XYZ_OSC.0_1024.hdf5.tar.bz2',
    'http://opendata.deepsig.io/datasets/2018.01/GOLD_XYZ_OSC.0_1024.hdf5.tar.bz2',
]

_MANUAL = """
─────────────────────────────────────────────────────────────────────────
  Automatic download failed.  Get the file manually:

  Option A — Kaggle (recommended):
    1.  Create a free account at  https://www.kaggle.com
    2.  Go to Account → Create New API Token → save kaggle.json to
        ~/.kaggle/kaggle.json  (chmod 600)
    3.  pip install kaggle
    4.  Run this script again

    Or download directly from the browser:
        https://www.kaggle.com/datasets/pinxau1000/radioml2018
    and place GOLD_XYZ_OSC.0001_1024.hdf5 in the data/ directory.

  Option B — DeepSig registration:
    1.  Go to  https://www.deepsig.ai/datasets
    2.  Register and download  GOLD_XYZ_OSC.0_1024.hdf5  (~3.5 GB)
    3.  Place the file in the data/ directory.

  Then train:
    uv run --with torch --with onnxscript scripts/train_modclass.py \\
        --data data/<filename>.hdf5
─────────────────────────────────────────────────────────────────────────
"""


def _find_existing(out_dir: str) -> str | None:
    for fname in _FNAMES:
        p = os.path.join(out_dir, fname)
        if os.path.exists(p):
            return p
    return None


def _try_kaggle(out_dir: str) -> str | None:
    """Download via kaggle CLI if available and configured."""
    kaggle = None
    for candidate in ('kaggle', os.path.expanduser('~/.local/bin/kaggle')):
        if os.path.exists(candidate) or _which(candidate):
            kaggle = candidate
            break
    if kaggle is None:
        try:
            import kaggle as _  # noqa: F401
            kaggle = sys.executable.replace('python', 'kaggle').split()[0]
        except ImportError:
            pass
    if kaggle is None:
        print('  kaggle CLI not found.  Install with:  pip install kaggle')
        return None

    token = os.path.expanduser('~/.kaggle/kaggle.json')
    if not os.path.exists(token):
        print(f'  kaggle.json not found at {token}')
        print('  Create an API token at https://www.kaggle.com/settings/account')
        return None

    print(f'  Downloading via kaggle CLI from {_KAGGLE_DATASET} …')
    os.makedirs(out_dir, exist_ok=True)
    result = subprocess.run(
        [kaggle, 'datasets', 'download',
         '-d', _KAGGLE_DATASET,
         '-p', out_dir,
         '--unzip'],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f'  kaggle download failed (exit {result.returncode})')
        return None
    return _find_existing(out_dir)


def _try_direct(out_dir: str) -> str | None:
    """Try known direct URLs as a last resort."""
    os.makedirs(out_dir, exist_ok=True)
    for url in _DIRECT_URLS:
        out_tmp = os.path.join(out_dir, os.path.basename(url))
        print(f'  Trying {url} …')
        try:
            urllib.request.urlretrieve(url, out_tmp, reporthook=_progress)
            print()
        except urllib.error.URLError as e:
            print(f'\n  Failed: {e}')
            if os.path.exists(out_tmp):
                os.remove(out_tmp)
            continue

        if out_tmp.endswith('.bz2'):
            print(f'  Extracting …')
            with tarfile.open(out_tmp, 'r:bz2') as tf:
                tf.extractall(out_dir)
            os.remove(out_tmp)

        found = _find_existing(out_dir)
        if found:
            return found

    return None


def _progress(count, block_size, total):
    if total <= 0:
        return
    done = count * block_size
    pct  = min(100.0, done / total * 100)
    mb   = done / 1_048_576
    tot  = total / 1_048_576
    bar  = '█' * int(pct / 2) + '·' * (50 - int(pct / 2))
    print(f'\r  {bar}  {mb:.0f}/{tot:.0f} MB  {pct:.0f}%', end='', flush=True)


def _which(cmd):
    import shutil
    return shutil.which(cmd)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--out', default=_DEFAULT_OUT,
                        help='Directory to save the HDF5 file (default: data/)')
    args = parser.parse_args()
    out_dir = os.path.abspath(args.out)

    # Already have it?
    found = _find_existing(out_dir)
    if found:
        size_gb = os.path.getsize(found) / 1e9
        print(f'Already present: {found}  ({size_gb:.1f} GB)')
        print(f'\nTrain with:\n  uv run --with torch --with onnxscript '
              f'scripts/train_modclass.py --data {found}')
        return

    print('RadioML 2018.01a downloader')
    print(f'Output directory: {out_dir}\n')

    found = _try_kaggle(out_dir)
    if found is None:
        found = _try_direct(out_dir)

    if found:
        size_gb = os.path.getsize(found) / 1e9
        print(f'\nDownloaded: {found}  ({size_gb:.1f} GB)')
        print(f'\nTrain with:\n  uv run --with torch --with onnxscript '
              f'scripts/train_modclass.py --data {found}')
    else:
        print(_MANUAL)
        sys.exit(1)


if __name__ == '__main__':
    main()
