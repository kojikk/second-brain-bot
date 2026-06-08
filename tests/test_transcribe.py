"""Транскрипция голосовых: конвертация OGG→WAV и вызов API."""
import io
import pytest
from unittest.mock import AsyncMock, MagicMock

import transcribe


class _FakeClient:
    def __init__(self, text: str = "тест"):
        self.audio = MagicMock()
        self.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text=text)
        )


@pytest.mark.asyncio
async def test_transcribe_returns_text(monkeypatch):
    """WAV-ввод напрямую уходит в API и возвращает текст."""
    fake = _FakeClient("привет мир")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)

    result = await transcribe.transcribe(b"fake_wav_data", "voice.wav")

    assert result == "привет мир"
    fake.audio.transcriptions.create.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_ogg_is_converted_to_wav(monkeypatch):
    """OGG должен конвертироваться: API получает файл с расширением .wav."""
    fake = _FakeClient("распознанный текст")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)
    monkeypatch.setattr(transcribe, "_to_wav", lambda audio: b"converted_wav_bytes")

    result = await transcribe.transcribe(b"ogg_data", "voice.ogg")

    assert result == "распознанный текст"
    kwargs = fake.audio.transcriptions.create.call_args.kwargs
    sent_file = kwargs["file"]
    assert sent_file.name.endswith(".wav"), "apinet требует WAV, не OGG"
    assert sent_file.read() == b"converted_wav_bytes"


@pytest.mark.asyncio
async def test_transcribe_opus_also_converted(monkeypatch):
    """Расширение .opus тоже проходит через конвертацию."""
    fake = _FakeClient("опус текст")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)
    converted = []
    monkeypatch.setattr(transcribe, "_to_wav", lambda a: converted.append(a) or b"wav")

    await transcribe.transcribe(b"opus_bytes", "voice.opus")

    assert converted, "_to_wav должен был вызваться для .opus"


def test_to_wav_raises_on_invalid_audio():
    """ffmpeg возвращает ненулевой код на мусорных данных → RuntimeError."""
    with pytest.raises(RuntimeError, match="ffmpeg"):
        transcribe._to_wav(b"this is not audio at all, just garbage bytes")


@pytest.mark.asyncio
async def test_transcribe_empty_result(monkeypatch):
    """Пустой ответ API возвращается как пустая строка, не None."""
    fake = _FakeClient("")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)

    result = await transcribe.transcribe(b"wav_data", "voice.wav")

    assert result == ""
