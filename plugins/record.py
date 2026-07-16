import json
import os
import numpy as np
from datetime import datetime, timezone
from core import Decoder, AppState


class RecordDecoder(Decoder):
    name            = 'record'
    key             = 'r'
    key_help        = 'o=path'
    min_sample_rate = 250_000

    def __init__(self):
        self._path        = None   # None = auto-generate on first process()
        self._active_path = None   # base path (no extension) while recording
        self._file        = None   # open data file handle
        self._predecessor = None   # plugin whose hooks we delegate to; None = raw IQ
        self._mode        = None   # 'sigmf' or the predecessor's record_ext string
        self._bytes       = 0
        self._error       = None
        self._sigmf_meta  = None   # dict written to .sigmf-meta on close

    def set_path(self, path):
        self._path = path or None

    def start(self, state: AppState) -> None:
        self._bytes      = 0
        self._error      = None
        self._file       = None
        self._predecessor = None
        self._mode       = None
        self._sigmf_meta = None

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if self._error:
            return {'error': self._error, 'bytes': self._bytes,
                    'path': self._active_path, 'mode': self._mode or 'err'}

        # _prev_plugin is injected by main.py's pipeline loop so we always know
        # the immediately preceding active plugin, not just any upstream result.
        predecessor = (results or {}).get('_prev_plugin')

        # Open output file(s) on the first call; mode is locked at that point.
        if self._file is None:
            if predecessor is not None and predecessor.record_ext is not None:
                self._predecessor = predecessor
                self._mode        = predecessor.record_ext
                base = self._path or _default_path(self._mode)
                self._active_path = base
                try:
                    self._file = self._predecessor.record_open(base)
                except OSError as e:
                    self._error = str(e)
                    return {'error': self._error}
            else:
                # Raw IQ → SigMF
                self._predecessor = None
                self._mode        = 'sigmf'
                base = _sigmf_base(self._path or _default_path('sigmf'))
                self._active_path = base
                data_path = base + '.sigmf-data'
                try:
                    self._file = open(data_path, 'wb')
                except OSError as e:
                    self._error = str(e)
                    return {'error': self._error}
                self._sigmf_meta = _build_meta(state)

        # Write one frame.
        if self._predecessor is None:
            iq = samples.astype(np.complex64)
            self._file.write(iq.tobytes())
            self._bytes += iq.nbytes
        else:
            pred_result = (results or {}).get(self._predecessor.name)
            if pred_result is not None:
                self._bytes += self._predecessor.record_write(self._file, pred_result)

        return {
            'bytes': self._bytes,
            'path':  self._active_path,
            'mode':  self._mode,
        }

    def stop(self) -> None:
        if self._file:
            if self._predecessor is not None:
                self._predecessor.record_close(self._file)
            else:
                self._file.close()
                # Write the companion .sigmf-meta file now that we know the
                # total sample count (bytes / 8 bytes per complex64 sample).
                if self._sigmf_meta is not None and self._active_path:
                    self._sigmf_meta['annotations'] = [{
                        'core:sample_start': 0,
                        'core:sample_count': self._bytes // 8,
                    }]
                    meta_path = self._active_path + '.sigmf-meta'
                    try:
                        with open(meta_path, 'w') as mf:
                            json.dump(self._sigmf_meta, mf, indent=2)
                    except OSError:
                        pass
            self._file = None
        self._bytes       = 0
        self._active_path = None
        self._error       = None
        self._predecessor = None
        self._mode        = None
        self._sigmf_meta  = None

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
        name = os.path.basename(result['path'] or '')
        if len(name) > 24:
            name = '…' + name[-23:]
        return '[REC/{} {} {:.1f}MB] '.format(result['mode'], name, mb)


# ── helpers ────────────────────────────────────────────────────────────────────

def _default_path(ext: str) -> str:
    return 'sdrterm_{}.{}'.format(datetime.now().strftime('%d-%m-%Y_%H%M%S'), ext)


def _sigmf_base(path: str) -> str:
    """Strip any SigMF or legacy IQ extension to get a clean base path."""
    for suffix in ('.sigmf-data', '.sigmf-meta', '.sigmf', '.iq'):
        if path.lower().endswith(suffix):
            return path[: -len(suffix)]
    return path


def _build_meta(state: AppState) -> dict:
    return {
        'global': {
            'core:datatype':    'cf32_le',
            'core:sample_rate': state.bw_hz,
            'core:version':     '1.0.0',
            'core:recorder':    'SDRTerm',
        },
        'captures': [{
            'core:sample_start': 0,
            'core:frequency':    state.center_hz,
            'core:datetime':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        }],
        'annotations': [],
    }
