import socket
import select
import queue
import struct
import threading
import numpy as np
from core import Decoder, AppState


class RtlTcpPassiveDecoder(Decoder):
    name            = 'rtl-tcp-passive'
    key             = 't'
    key_help        = 'o=port'
    min_sample_rate = 250_000
    realtime        = False

    _MAGIC        = b'RTL0'
    _TUNER_R820T2 = 5

    def __init__(self):
        self._port        = 1234
        self._server_sock = None
        self._client_sock = None
        self._client_addr = None
        self._bytes_sent  = 0
        self._error       = None
        self._lock        = threading.Lock()
        self._queue       = None
        self._stop_evt    = threading.Event()
        self._thread      = None

    def set_path(self, value):
        try:
            port = int(value)
            if 1 <= port <= 65535:
                self._port = port
        except (TypeError, ValueError):
            pass

    def start(self, state: AppState) -> None:
        self._stop_evt.clear()
        self._queue      = queue.Queue(maxsize=16)
        self._bytes_sent = 0
        self._error      = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', self._port))
            sock.listen(1)
            sock.settimeout(0.5)
            self._server_sock = sock
        except OSError as e:
            self._error = str(e)
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with self._lock:
                self._client_sock = conn
                self._client_addr = addr[0]
                self._bytes_sent  = 0

            header = (self._MAGIC
                      + struct.pack('>I', self._TUNER_R820T2)
                      + struct.pack('>I', 0))
            try:
                conn.sendall(header)
            except OSError:
                conn.close()
                with self._lock:
                    self._client_sock = None
                    self._client_addr = None
                continue

            while not self._stop_evt.is_set():
                # Drain incoming command bytes (passive mode: discard them all)
                r, _, _ = select.select([conn], [], [], 0)
                if r:
                    try:
                        data = conn.recv(4096)
                        if not data:
                            break   # clean disconnect
                    except OSError:
                        break

                try:
                    chunk = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    conn.sendall(chunk)
                    with self._lock:
                        self._bytes_sent += len(chunk)
                except OSError:
                    break

            conn.close()
            with self._lock:
                self._client_sock = None
                self._client_addr = None
            # Drain stale chunks so next client starts fresh
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def process(self, samples: np.ndarray, state: AppState,
                results: dict = None, sdr=None) -> dict:
        if self._error:
            return {'port': self._port, 'client': None, 'bytes': 0,
                    'error': self._error}

        with self._lock:
            connected = self._client_sock is not None

        if connected:
            iq  = samples.astype(np.complex64)
            arr = np.empty(len(iq) * 2, dtype=np.uint8)
            arr[0::2] = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
            arr[1::2] = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
            try:
                self._queue.put_nowait(arr.tobytes())
            except queue.Full:
                pass   # client too slow — drop chunk rather than back-pressuring

        with self._lock:
            return {
                'port':   self._port,
                'client': self._client_addr,
                'bytes':  self._bytes_sent,
            }

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            for s in (self._client_sock, self._server_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            self._client_sock = None
            self._server_sock = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def handle_key(self, key: int, state: AppState, sdr) -> bool:
        if key == ord('o'):
            state.path_input        = str(self._port)
            state.path_input_target = self.name
            return True
        return False

    def status_text(self, state: AppState, result: dict) -> str:
        if not result:
            return ''
        if 'error' in result:
            return '[RTL-TCP error: {}] '.format(result['error'][:24])
        port   = result['port']
        client = result['client']
        if client is None:
            return '[RTL-TCP :{} waiting] '.format(port)
        mb = result['bytes'] / 1_048_576
        return '[RTL-TCP :{} {} {:.1f}MB] '.format(port, client, mb)
