"""Band-limited sample-rate conversion for the phone line (numpy only).

audioop.ratecv has NO low-pass filter: going 24 kHz -> 8 kHz it folds everything above 4 kHz (the hiss of s/sh/ch) back into the audible
band as a harsh, "rattling" noise, and 8 kHz -> 16 kHz it adds spectral images that Gemini's speech-to-text has to hear through.
These classes low-pass with a Kaiser-window FIR first. They are stateful (chunks of any size, no clicks at the joins) and
zero-delay (the filter's own delay is compensated).
"""
import numpy as np


def _lowpass(taps: int, cutoff_hz: float, fs: float, beta: float) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0
    h = np.sinc(2.0 * cutoff_hz / fs * n) * np.kaiser(taps, beta)
    return (h / h.sum()).astype(np.float64)


H_DOWN = _lowpass(127, 3650.0, 24000.0, 6.0)          # 24 kHz -> 8 kHz: pass to ~3.3 kHz, stop from ~4.0 kHz
H_UP = _lowpass(95, 3650.0, 16000.0, 6.0) * 2.0        # 8 kHz -> 16 kHz (x2 makes up for the inserted zeros)


def _to_int16(y: np.ndarray) -> bytes:
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


class Down3:
    """24 kHz 16-bit mono PCM -> 8 kHz."""
    def __init__(self):
        self.reset()

    def reset(self):
        self._buf = np.zeros((len(H_DOWN) - 1) // 2)

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) < 2:
            return b""
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64)
        buf = np.concatenate((self._buf, x))
        if len(buf) < len(H_DOWN):
            self._buf = buf
            return b""
        y = np.convolve(buf, H_DOWN, mode="valid")[::3]
        self._buf = buf[3 * len(y):]
        return _to_int16(y)


class Up2:
    """8 kHz 16-bit mono PCM -> 16 kHz."""
    def __init__(self):
        self.reset()

    def reset(self):
        self._buf = np.zeros((len(H_UP) - 1) // 2)

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) < 2:
            return b""
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64)
        up = np.zeros(len(x) * 2)
        up[::2] = x
        buf = np.concatenate((self._buf, up))
        if len(buf) < len(H_UP):
            self._buf = buf
            return b""
        y = np.convolve(buf, H_UP, mode="valid")
        self._buf = buf[len(y):]
        return _to_int16(y)


def down3(pcm24: bytes) -> bytes:
    """One-shot 24 kHz -> 8 kHz (recordings, tests)."""
    return Down3().process(pcm24 + b"\x00\x00" * len(H_DOWN))[: len(pcm24) // 6 * 2]


def up2(pcm8: bytes) -> bytes:
    """One-shot 8 kHz -> 16 kHz (classifier audio)."""
    return Up2().process(pcm8 + b"\x00\x00" * len(H_UP))[: len(pcm8) * 2]
