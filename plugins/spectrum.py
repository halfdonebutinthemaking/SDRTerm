import numpy as np
from core import Decoder, AppState, FFT_BINS, N_AVG, WINDOW, correct_iq


class SpectrumDecoder(Decoder):
    name            = 'spectrum'
    min_sample_rate = 250_000
    # no key — always active, never toggled

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None) -> dict:
        s      = samples[:FFT_BINS * N_AVG]
        frames = s.reshape(N_AVG, FFT_BINS)
        power  = np.zeros(FFT_BINS)
        for frame in frames:
            if state.iq_corr:
                frame = correct_iq(frame)
            fft_out = np.fft.fftshift(np.fft.fft(frame * WINDOW, FFT_BINS))
            power  += np.abs(fft_out) ** 2
        mags_db = 10 * np.log10(power / N_AVG / FFT_BINS ** 2 + 1e-20)
        freqs   = np.linspace(state.center_hz - state.bw_hz / 2,
                              state.center_hz + state.bw_hz / 2, FFT_BINS)
        return {'freqs': freqs, 'mags_db': mags_db}
