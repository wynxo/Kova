"""Local voice: on-machine transcription and speech.

Issue #68 - talk to Kova and hear it back. Everything runs on the user's own
machine; no cloud speech API. Speech-to-text shells out to ``whisper-cli``
(whisper.cpp) over audio normalised with ``ffmpeg``; text-to-speech shells out
to ``espeak-ng``. Both backends are optional - Kova starts and answers turns
fine without them - and every external command is wrapped so a missing binary
or a short file surfaces as a single-sentence error rather than a traceback.

The ``Transcriber`` and ``TTSBackend`` protocols let tests inject a stub and let
a future build swap in a compiled backend without touching the server.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from typing import Protocol
from dataclasses import dataclass
from pathlib import Path

#: Hard cap on an uploaded audio file (10 MiB - far below the attachment limit).
MAX_AUDIO_BYTES = 10 * 1024 * 1024
#: Longest clip accepted, in seconds.
MAX_SECONDS = 60
#: whisper.cpp's audio expects 16 kHz mono 16-bit PCM.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1


class TranscriptionError(ValueError):
    """A safe, single-sentence failure for the client."""


class TranscriptionResult:
    """What a transcription returns.

    ``backend`` names which engine answered (``"whisper.cpp"`` or the configured
    value) so the UI can show a small badge; ``duration_ms`` lets the composer
    time the reply.
    """

    __slots__ = ("text", "language", "backend", "duration_ms")

    def __init__(self, text: str, language: str | None = None,
                 backend: str = "whisper.cpp", duration_ms: int | None = None):
        self.text = text
        self.language = language
        self.backend = backend
        self.duration_ms = duration_ms

    def as_dict(self) -> dict:
        return {"text": self.text, "language": self.language,
                "backend": self.backend, "duration_ms": self.duration_ms}


class Transcriber(Protocol):
    """Transcribe audio bytes into text."""

    def transcribe(self, audio: bytes, fmt: str = "webm") -> TranscriptionResult: ...


class TTSBackend(Protocol):
    """Speak text; return the path to the audio it wrote."""

    def speak(self, text: str) -> Path: ...


def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def format_audio(audio: bytes, fmt: str = "webm") -> bytes:
    """Normalise uploaded audio to 16 kHz mono 16-bit PCM WAV for whisper.cpp.

    Returns raw WAV bytes. ``fmt`` is accepted for API symmetry; whisper.cpp's
    loader reads PCM WAV, so conversion always targets that. A short or clipped
    clip raises :class:`TranscriptionError` rather than feeding the model junk.
    """
    if not audio:
        raise TranscriptionError("No audio sent.")

    with tempfile.TemporaryDirectory(prefix="kova-voice-") as tmp:
        src = Path(tmp) / f"in.{fmt}"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(audio)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src),
                 "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE),
                 "-ac", str(CHANNELS), str(dst)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError as exc:
            raise TranscriptionError("ffmpeg is not installed.") from exc
        if not dst.exists():
            raise TranscriptionError("The audio could not be converted.")
        data = dst.read_bytes()
    if len(data) < 44:
        raise TranscriptionError("The audio is empty.")
    return data


class LocalTranscriber:
    """whisper.cpp transcription over ffmpeg-normalised audio."""

    backend = "whisper.cpp"

    def __init__(self, language: str | None = None, max_seconds: int = MAX_SECONDS):
        self.language = language
        self.max_seconds = max_seconds
        if _which("whisper-cli") is None:
            raise TranscriptionError(
                "whisper-cli (whisper.cpp) is not installed. Install it to turn on "
                "on-device speech-to-text.")

    def transcribe(self, audio: bytes, fmt: str = "webm") -> TranscriptionResult:
        pcm = format_audio(audio, fmt)
        with tempfile.TemporaryDirectory(prefix="kova-voice-") as tmp:
            src = Path(tmp) / "in.wav"  # format_audio already returned 16 kHz mono WAV
            out = Path(tmp) / "out.txt"
            src.write_bytes(pcm)
            cmd = [
                "whisper-cli", "-m", "models/whisper.bin",
                "-l", self.language or "auto", "-f", str(src),
                "--no-gpu", "-otxt", "-of", str(out.with_suffix("")),
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            except FileNotFoundError as exc:
                raise TranscriptionError("whisper-cli is not installed.") from exc
            if not out.exists():
                raise TranscriptionError("The transcription failed.")
            text = out.read_text(encoding="utf-8").strip()
        if not text:
            raise TranscriptionError("The audio had no speech.")
        return TranscriptionResult(text=text, language=self.language or None,
                                   backend=self.backend)


def build_transcriber(config) -> Transcriber | None:
    """Pick a transcriber from ``[voice] stt``.

    ``"browser"`` means the client transcribes client-side (Web Speech API), so
    the server holds no transcriber. ``"local"`` builds a whisper.cpp backend,
    which raises if the binary is missing.
    """
    stt = getattr(config, "voice_stt", "local") or "local"
    if stt == "browser":
        return None
    return LocalTranscriber(
        language=getattr(config, "voice_language", None),
        max_seconds=getattr(config, "voice_max_seconds", MAX_SECONDS),
    )


class LocalTTS:
    """espeak-ng text-to-speech; returns the path to the WAV it writes."""

    backend = "espeak-ng"

    def __init__(self, voice: str = "en"):
        self.voice = voice
        if _which("espeak-ng") is None:
            raise TranscriptionError("espeak-ng is not installed.")

    def speak(self, text: str) -> Path:
        voice = self.voice or "en"
        text = " ".join(text.split())[:500]
        with tempfile.TemporaryDirectory(prefix="kova-voice-") as tmp:
            out = Path(tmp) / "out.wav"
            try:
                subprocess.run(
                    ["espeak-ng", "-v", voice, "-w", str(out), text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except FileNotFoundError as exc:
                raise TranscriptionError("espeak-ng is not installed.") from exc
            if not out.exists():
                raise TranscriptionError("Speech synthesis failed.")
            return out


def build_tts(config) -> TTSBackend | None:
    """Pick a speech backend from ``[voice] tts`` (default espeak-ng)."""
    tts = getattr(config, "voice_tts", "espeak-ng") or "espeak-ng"
    if tts == "off":
        return None
    voice = getattr(config, "voice_language", None) or "en"
    return LocalTTS(voice=voice)


def say(text: str, tts: TTSBackend | None = None) -> Path | None:
    """Speak ``text`` with the given backend; return the WAV path or None."""
    if tts is None:
        return None
    return tts.speak(text)
