# Базовый образ: Python 3.12 (стабильный, без проблем с PTB)
FROM python:3.12-slim

# Устанавливаем зависимости системы (для asyncpg нужен libpq)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Сначала копируем requirements, чтобы кэшировалась установка пакетов
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]