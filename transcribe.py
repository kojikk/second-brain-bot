"""
Транскрипция голосовых сообщений Telegram.

apinet.cloud НЕ реализует /audio/transcriptions (возвращает «not implemented»).
Вместо этого аудио отправляется через /chat/completions с content-type
input_audio — тот же эндпоинт, что работает для текстового чата.

Голосовые Telegram приходят в OGG/Opus; конвертируем в WAV 16kHz через ffmpeg
(добавлен в Dockerfile), потом base64-кодируем и кладём в input_audio.
"""
import base64
import logging
import subprocess

from openai import AsyncOpenAI

from config import CLAUDE_API_KEY, CLAUDE_BASE_URL, TRANSCRIBE_MODEL

logger = logging.getLogger(__name__)

_llm: AsyncOpenAI | None = None
_OGG_EXTS = {"ogg", "oga", "opus"}

_PROMPT = (
    "Transcribe this voice message verbatim in the original language. "
    "Output only the transcription text, no explanations or commentary."
)


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

    Использует chat/completions с multimodal input_audio вместо
    /audio/transcriptions, который на apinet не реализован.
    Бросает исключение при сетевой/биллинговой ошибке.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    if ext in _OGG_EXTS:
        audio = _to_wav(audio)

    audio_b64 = base64.b64encode(audio).decode()
    resp = await _client().chat.completions.create(
        model=TRANSCRIBE_MODEL,
        max_tokens=1024,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_b64, "format": "wav"},
                },
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    logger.info("transcribe: %d байт аудио → %d символов текста", len(audio), len(text))
    return text
