"""Voice helpers: the ffmpeg step must work with real file names, not just stubs."""

import subprocess
from pathlib import Path

import pytest

from kova import voice


def _fake_ffmpeg(calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"RIFF" + b"\0" * 60)  # a "converted" WAV
        return subprocess.CompletedProcess(cmd, 0)
    return run


def test_format_audio_builds_input_name_from_format(monkeypatch):
    calls = []
    monkeypatch.setattr(voice.subprocess, "run", _fake_ffmpeg(calls))
    data = voice.format_audio(b"opus-bytes", "webm")
    assert data.startswith(b"RIFF")
    assert Path(calls[0][calls[0].index("-i") + 1]).name == "in.webm"


def test_format_audio_rejects_empty_input():
    with pytest.raises(voice.TranscriptionError):
        voice.format_audio(b"", "webm")


def test_local_transcriber_reads_whisper_output(monkeypatch):
    monkeypatch.setattr(voice, "_which", lambda *names: "/usr/bin/" + names[0])

    def run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF" + b"\0" * 60)
        else:  # whisper-cli: "-of <base>" with "-otxt" writes <base>.txt
            Path(cmd[cmd.index("-of") + 1] + ".txt").write_text(" hello kova \n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(voice.subprocess, "run", run)
    result = voice.LocalTranscriber().transcribe(b"opus-bytes", "webm")
    assert result.text == "hello kova"
