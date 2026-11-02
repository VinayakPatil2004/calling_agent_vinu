"""
Audio Codec and Resampling Utilities for VoiceLink / Exotel Telephony.
Handles ITU-T G.711 A-law (PCMA) encoding/decoding and 8kHz <-> 16kHz sample rate conversion.
High performance, zero-dependency implementation using precomputed lookup tables.
"""
import io
import wave
import struct
from typing import List, Tuple, Optional


def _build_alaw_to_linear_table() -> List[int]:
    """Generates 256-entry lookup table decoding G.711 A-law to 16-bit signed linear PCM."""
    table = []
    for a in range(256):
        a_val = a ^ 0x55
        sign = a_val & 0x80
        exponent = (a_val & 0x70) >> 4
        mantissa = a_val & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        table.append(sample if sign != 0 else -sample)
    return table


def _build_linear_to_alaw_table() -> bytes:
    """Generates 65536-entry lookup table encoding 16-bit signed linear PCM (-32768..32767) to A-law."""
    table = bytearray(65536)
    for i in range(65536):
        pcm_val = i - 32768
        sign = 0
        if pcm_val < 0:
            pcm_val = -pcm_val - 1
            sign = 0x80
        if pcm_val > 32767:
            pcm_val = 32767

        if pcm_val >= 4096:
            exponent = 7 if pcm_val >= 16384 else (6 if pcm_val >= 8192 else 5)
        elif pcm_val >= 512:
            exponent = 4 if pcm_val >= 2048 else (3 if pcm_val >= 1024 else 2)
        elif pcm_val >= 256:
            exponent = 1
        else:
            exponent = 0

        if exponent == 0:
            mantissa = (pcm_val >> 4) & 0x0F
        else:
            mantissa = (pcm_val >> (exponent + 3)) & 0x0F

        table[i] = (sign | (exponent << 4) | mantissa) ^ 0xD5
    return bytes(table)


# Precomputed lookup tables for microsecond-level conversion
_ALAW_TO_LINEAR_LUT = _build_alaw_to_linear_table()
_LINEAR_TO_ALAW_LUT = _build_linear_to_alaw_table()


def alaw_to_linear(alaw_bytes: bytes) -> bytes:
    """
    Decodes G.711 A-law (PCMA) 8-bit mono audio into 16-bit signed linear PCM (little-endian).
    Length doubles: 1 byte of PCMA -> 2 bytes of PCM.
    """
    if not alaw_bytes:
        return b""
    samples = [_ALAW_TO_LINEAR_LUT[b] for b in alaw_bytes]
    return struct.pack(f"<{len(samples)}h", *samples)


def linear_to_alaw(pcm_bytes: bytes) -> bytes:
    """
    Encodes 16-bit signed linear PCM (little-endian) into G.711 A-law (PCMA) 8-bit mono audio.
    Length halves: 2 bytes of PCM -> 1 byte of PCMA.
    """
    count = len(pcm_bytes) // 2
    if count == 0:
        return b""
    samples = struct.unpack(f"<{count}h", pcm_bytes)
    return bytes([_LINEAR_TO_ALAW_LUT[s + 32768] for s in samples])


def resample_8k_to_16k(pcm8k_bytes: bytes) -> bytes:
    """
    Upsamples 8,000 Hz 16-bit mono PCM to 16,000 Hz 16-bit mono PCM.
    Uses linear interpolation for artifact-free speech recognition input.
    """
    count = len(pcm8k_bytes) // 2
    if count == 0:
        return b""
    samples = struct.unpack(f"<{count}h", pcm8k_bytes)
    out = []
    for i in range(count - 1):
        s0 = samples[i]
        s1 = samples[i + 1]
        out.extend([s0, (s0 + s1) // 2])
    out.extend([samples[-1], samples[-1]])
    return struct.pack(f"<{len(out)}h", *out)


def resample_16k_to_8k(pcm16k_bytes: bytes) -> bytes:
    """
    Downsamples 16,000 Hz 16-bit mono PCM to 8,000 Hz 16-bit mono PCM.
    Uses 2-sample average to prevent aliasing.
    """
    count = len(pcm16k_bytes) // 2
    if count < 2:
        return b""
    samples = struct.unpack(f"<{count}h", pcm16k_bytes)
    out = [(samples[i] + samples[i + 1]) // 2 for i in range(0, count - 1, 2)]
    return struct.pack(f"<{len(out)}h", *out)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """
    Wraps raw 16-bit signed mono PCM bytes in a standard RIFF WAV container.
    Used to pass processed telephony audio into Sarvam STT transcribe().
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)  # Mono
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buffer.getvalue()


def wav_to_pcm(wav_bytes: bytes) -> Tuple[bytes, int]:
    """
    Unwraps raw 16-bit mono PCM audio and sample rate from a RIFF WAV container.
    Returns (raw_pcm_bytes, sample_rate).
    """
    buffer = io.BytesIO(wav_bytes)
    with wave.open(buffer, "rb") as wf:
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        return frames, sample_rate


def chunk_pcma(pcma_bytes: bytes, chunk_size: int = 320) -> List[bytes]:
    """
    Splits PCMA audio bytes into uniform chunks for streaming to VoiceLink/Exotel.
    Exotel specifies chunk sizes must be multiples of 320 bytes (~40ms of 8kHz audio).
    """
    if not pcma_bytes:
        return []
    chunks = []
    total_len = len(pcma_bytes)
    for i in range(0, total_len, chunk_size):
        chunk = pcma_bytes[i:i + chunk_size]
        # Pad final chunk with A-law silence (0xD5) to maintain 320-byte alignment
        if len(chunk) < chunk_size:
            chunk = chunk + (b"\xD5" * (chunk_size - len(chunk)))
        chunks.append(chunk)
    return chunks

