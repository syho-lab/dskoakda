# Используем легкую версию Python
FROM python:3.11-slim

# Устанавливаем системные зависимости (FFmpeg нужен для 4K и склейки аудио)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Создаем рабочую папку
WORKDIR /app

# Копируем файлы проекта
COPY . .

# Устанавливаем библиотеки Python
RUN pip install --no-cache-dir -r requirements.txt

# Создаем папку для загрузок
RUN mkdir downloads

# Команда запуска
CMD ["python", "bot.py"]
