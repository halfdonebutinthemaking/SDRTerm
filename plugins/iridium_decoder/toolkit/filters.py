"""RRC / RC / Gaussian FIR filters — port of extractor-python/filters.py.

Originally from commpy (GPLv3).  Python 3 compatible: only difference from
upstream is int() coercions around `t == Ts/(2*alpha)` divisions, which
Python 2 handled implicitly.
"""
import numpy as np

__all__ = ['rcosfilter', 'rrcosfilter', 'gaussianfilter']


def rcosfilter(N, alpha, Ts, Fs):
    T_delta = 1 / float(Fs)
    time_idx = (np.arange(N) - N // 2) * T_delta
    h_rc = np.zeros(N, dtype=float)
    for x in range(N):
        t = (x - N // 2) * T_delta
        if t == 0.0:
            h_rc[x] = 1.0
        elif alpha != 0 and t == Ts / (2 * alpha):
            h_rc[x] = (np.pi / 4) * (np.sin(np.pi * t / Ts) / (np.pi * t / Ts))
        elif alpha != 0 and t == -Ts / (2 * alpha):
            h_rc[x] = (np.pi / 4) * (np.sin(np.pi * t / Ts) / (np.pi * t / Ts))
        else:
            h_rc[x] = (np.sin(np.pi * t / Ts) / (np.pi * t / Ts)) * (
                np.cos(np.pi * alpha * t / Ts) /
                (1 - (((2 * alpha * t) / Ts) * ((2 * alpha * t) / Ts))))
    return time_idx, h_rc


def rrcosfilter(N, alpha, Ts, Fs):
    T_delta = 1 / float(Fs)
    time_idx = (np.arange(N) - N // 2) * T_delta
    h_rrc = np.zeros(N, dtype=float)
    for x in range(N):
        t = (x - N // 2) * T_delta
        if t == 0.0:
            h_rrc[x] = 1.0 - alpha + (4 * alpha / np.pi)
        elif alpha != 0 and t == Ts / (4 * alpha):
            h_rrc[x] = (alpha / np.sqrt(2)) * (
                ((1 + 2 / np.pi) * (np.sin(np.pi / (4 * alpha)))) +
                ((1 - 2 / np.pi) * (np.cos(np.pi / (4 * alpha)))))
        elif alpha != 0 and t == -Ts / (4 * alpha):
            h_rrc[x] = (alpha / np.sqrt(2)) * (
                ((1 + 2 / np.pi) * (np.sin(np.pi / (4 * alpha)))) +
                ((1 - 2 / np.pi) * (np.cos(np.pi / (4 * alpha)))))
        else:
            h_rrc[x] = (
                np.sin(np.pi * t * (1 - alpha) / Ts) +
                4 * alpha * (t / Ts) * np.cos(np.pi * t * (1 + alpha) / Ts)
            ) / (np.pi * t * (1 - (4 * alpha * t / Ts) *
                              (4 * alpha * t / Ts)) / Ts)
    return time_idx, h_rrc


def gaussianfilter(N, alpha, Ts, Fs):
    T_delta = 1 / float(Fs)
    time_idx = (np.arange(N) - N // 2) * T_delta
    h_gaussian = ((np.sqrt(np.pi) / alpha) *
                  np.exp(-((np.pi * time_idx / alpha) ** 2)))
    return time_idx, h_gaussian


def rectfilter(N, Ts, Fs):
    h_rect = np.ones(N)
    T_delta = 1 / float(Fs)
    time_idx = (np.arange(N) - N // 2) * T_delta
    return time_idx, h_rect
