"""
Транскрипция голосовых сообщений Telegram.

apinet.cloud НЕ принимает OGG/Opus на /audio/transcriptions — только WAV/MP3.
Поэтому голосовые (Telegram отдаёт OGG) конвертируются в WAV через ffmpeg
(системная зависимость, добавлена в Dockerfile) перед отправкой.

Модель: gemini-2.5-flash (у apinet нет whisper-канала; аудио распознаёт
мультимодальный Gemini). Идёт тем же ключом и egress'ом (singbox), что и чат.
"""
import io
import logging
import subprocess

from openai import AsyncOpenAI

from config import CLAUDE_API_KEY, CLAUDE_BASE_URL, TRANSCRIBE_MODEL

logger = logging.getLogger(__name__)

# Ленивая инициализация (как в agent): импорт без секретов не должен падать.
_llm: AsyncOpenAI | None = None

# Форматы, которые нужно конвертировать в WAV перед отправкой.
_OGG_EXTS = {"ogg", "oga", "opus"}


def _client() -> AsyncOpenAI:
    global _llm
    if _llm is None:
        _llm = AsyncOpenAI(api_key=CLAUDE_API_KEY, base_url=CLAUDE_BASE_URL)
    return _llm


def _to_wav(audio: bytes) -> bytes:
    """Конвертировать аудио в 16kHz mono WAV через ffmpeg (pipe→pipe).

    Блокирующий вызов — приемлемо для персонального бота с одним пользователем.
    Бросает RuntimeError при ненулевом коде возврата ffmpeg.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", "pipe:0",
            "-f", "wav", "-ar", "16000", "-ac", "1",
            "pipe:1",
        ],
        input=audio,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        err = result.stderr[-300:].decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg: {err}")
    return result.stdout


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """Распознать речь из аудио-байтов. Возвращает текст (может быть пустым).

    Бросает исключение при сетевой/биллинговой ошибке — вызывающий решает,
    как сообщить пользователю.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    if ext in _OGG_EXTS:
        audio = _to_wav(audio)
        filename = filename.rsplit(".", 1)[0] + ".wav"

    buf = io.BytesIO(audio)
    buf.name = filename  # openai-SDK берёт расширение из .name для content-type
    resp = await _client().audio.transcriptions.create(
        model=TRANSCRIBE_MODEL, file=buf,
    )
    text = (getattr(resp, "text", "") or "").strip()
    logger.info("transcribe: %d байт аудио → %d символов текста", len(audio), len(text))
    return text
