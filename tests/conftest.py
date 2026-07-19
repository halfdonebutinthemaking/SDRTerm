import numpy as np
import pytest
from collections import deque
from unittest.mock import MagicMock

from core import AppState, Device, BW_STEPS, FFT_BINS, N_AVG, CENTER_HZ


@pytest.fixture
def state():
    return AppState()


@pytest.fixture
def fake_sdr():
    sdr = MagicMock(spec=Device)
    sdr.supported_bandwidths = BW_STEPS
    sdr.sample_rate = BW_STEPS[-1]
    sdr.center_freq = CENTER_HZ
    sdr.gain = 0.0
    sdr.key_help = ''
    sdr.freq_min = 25_000_000.0
    sdr.freq_max = 1_766_000_000.0
    sdr.status_text.return_value = ''
    sdr.handle_key.return_value = False
    return sdr


@pytest.fixture
def fake_stdscr():
    scr = MagicMock()
    scr.getmaxyx.return_value = (50, 200)
    return scr


@pytest.fixture
def iq_samples():
    rng = np.random.default_rng(42)
    n = FFT_BINS * N_AVG
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)


@pytest.fixture(scope='session')
def registry():
    from plugins import load_plugins
    return load_plugins()
