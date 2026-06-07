FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости отдельным слоем для кэша.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Non-root: контейнер с сетевым доступом не должен бегать под root (H-4).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "telegram_bot.py"]
