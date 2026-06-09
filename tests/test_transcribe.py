"""Транскрипция голосовых: конвертация OGG→WAV и вызов chat/completions."""
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock

import transcribe


def _fake_client(text: str = "тест"):
    """Мок AsyncOpenAI с заглушкой chat.completions.create."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_transcribe_returns_text(monkeypatch):
    """WAV-ввод уходит в chat/completions и возвращает текст."""
    fake = _fake_client("привет мир")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)

    result = await transcribe.transcribe(b"fake_wav_data", "voice.wav")

    assert result == "привет мир"
    fake.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_ogg_is_converted_to_wav(monkeypatch):
    """OGG конвертируется: в input_audio уходит base64 от _to_wav."""
    fake = _fake_client("распознанный текст")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)
    monkeypatch.setattr(transcribe, "_to_wav", lambda audio: b"converted_wav_bytes")

    result = await transcribe.transcribe(b"ogg_data", "voice.ogg")

    assert result == "распознанный текст"
    call_kwargs = fake.chat.completions.create.call_args.kwargs
    content = call_kwargs["messages"][0]["content"]
    audio_part = next(p for p in content if p.get("type") == "input_audio")
    decoded = base64.b64decode(audio_part["input_audio"]["data"])
    assert decoded == b"converted_wav_bytes"
    assert audio_part["input_audio"]["format"] == "wav"


@pytest.mark.asyncio
async def test_transcribe_opus_also_converted(monkeypatch):
    """Расширение .opus тоже проходит через конвертацию."""
    fake = _fake_client("опус текст")
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
    fake = _fake_client("")
    monkeypatch.setattr(transcribe, "_client", lambda: fake)

    result = await transcribe.transcribe(b"wav_data", "voice.wav")

    assert result == ""
