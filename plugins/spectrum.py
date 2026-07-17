import numpy as np
from core import Decoder, AppState, FFT_BINS, N_AVG, WINDOW, DB_MIN, correct_iq


class SpectrumDecoder(Decoder):
    name            = 'spectrum'
    min_sample_rate = 250_000
    # no key — always active, never toggled

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        s      = samples[:FFT_BINS * N_AVG]
        frames = s.reshape(N_AVG, FFT_BINS)
        power  = np.zeros(FFT_BINS)
        for frame in frames:
            if state.iq_corr:
                frame = correct_iq(frame)
            fft_out = np.fft.fftshift(np.fft.fft(frame * WINDOW, FFT_BINS))
            power  += np.abs(fft_out) ** 2
        mags_db = 10 * np.log10(power / N_AVG / FFT_BINS ** 2 + 1e-20)

        # File-replay display alignment: the IQ file is always baseband-
        # referenced to its recorded centre (_file_center_hz).  When the
        # display centre (state.center_hz) is moved by follow mode or manual
        # entry, the signal stays at the same FFT bin but the axis labels
        # shift.  Roll mags_db so the signal appears at the column that
        # matches the new axis, padding the exposed edge with the noise floor.
        file_center = getattr(sdr, '_file_center_hz', None)
        if file_center is not None and file_center != state.center_hz:
            shift = round((file_center - state.center_hz) / state.bw_hz * FFT_BINS)
            if shift != 0:
                mags_db = np.roll(mags_db, shift)
                if shift > 0:
                    mags_db[:shift] = DB_MIN
                else:
                    mags_db[shift:] = DB_MIN

        freqs = np.linspace(state.center_hz - state.bw_hz / 2,
                            state.center_hz + state.bw_hz / 2, FFT_BINS)
        return {'freqs': freqs, 'mags_db': mags_db}
