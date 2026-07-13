import os
import wave
import numpy as np
from datetime import datetime
from core import Decoder, AppState, AUDIO_RATE


class RecordDecoder(Decoder):
    name            = 'record'
    key             = 'r'
    key_help        = 'o=path'
    min_sample_rate = 250_000

    def __init__(self):
        self._path        = None   # None = auto-generate on start
        self._active_path = None
        self._file        = None   # file handle or wave.Wave_write
        self._is_wav      = False
        self._bytes       = 0
        self._error       = None   # set when file open fails

    def set_path(self, path):
        self._path = path or None

    def start(self, state: AppState) -> None:
        self._bytes  = 0
        self._error  = None
        self._file   = None        # opened on first process() once mode is known

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if self._error:
            return {'error': self._error}

        # Find audio from any upstream plugin that exposes it
        audio = None
        if results:
            for r in results.values():
                if isinstance(r, dict) and 'audio' in r:
                    audio = r['audio']
                    break

        mode = 'audio' if audio is not None else 'iq'

        # Open file on first call so we know the mode
        if self._file is None:
            path = self._path or _default_path(mode)
            self._active_path = path
            self._is_wav = (mode == 'audio')
            try:
                if self._is_wav:
                    wf = wave.open(path, 'wb')
                    wf.setnchannels(1)
                    wf.setsampwidth(2)   # int16
                    wf.setframerate(AUDIO_RATE)
                    self._file = wf
                else:
                    self._file = open(path, 'wb')
            except OSError as e:
                self._error = str(e)
                return {'error': self._error}

        # Write
        if self._is_wav and audio is not None:
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            self._file.writeframes(pcm.tobytes())
            self._bytes += pcm.nbytes
        elif not self._is_wav:
            iq = samples.astype(np.complex64)
            self._file.write(iq.tobytes())
            self._bytes += iq.nbytes

        return {
            'bytes': self._bytes,
            'path':  self._active_path,
            'mode':  'wav' if self._is_wav else 'iq',
        }

    def stop(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
        self._bytes       = 0
        self._active_path = None
        self._error       = None

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('o'):
            state.path_input        = self._path or ''
            state.path_input_target = self.name
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        if 'error' in result:
            return '[REC error: {}] '.format(result['error'][:30])
        mb   = result['bytes'] / 1_048_576
        name = os.path.basename(result['path'])
        if len(name) > 24:
            name = '…' + name[-23:]
        return '[REC/{} {} {:.1f}MB] '.format(result['mode'], name, mb)


def _default_path(mode: str) -> str:
    ext = 'wav' if mode == 'audio' else 'iq'
    return 'sdrterm_{}.{}'.format(datetime.now().strftime('%d-%m-%Y_%H%M%S'), ext)
