import logging
import os
import threading
import json
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
import requests
from bs4 import BeautifulSoup
from flask import Flask

# --- НАСТРОЙКИ ---
# Получаем токен из переменных окружения (безопаснее для Render) или используем жестко заданный
TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН_ЕСЛИ_ЗАПУСКАЕШЬ_ЛОКАЛЬНО")

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- WEB SERVER ДЛЯ RENDER (ЧТОБЫ БОТ НЕ УМИРАЛ) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive!", 200

def run_flask():
    # Render выдает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# --- ЛОГИКА БОТА ---

async def start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        'Привет! 👋\n'
        'Я готов скачивать видео в 4K (с FFmpeg) на сервере Render!\n'
        'Отправь ссылку на YouTube или Pinterest.'
    )

async def handle_link(update: Update, context: CallbackContext) -> None:
    link = update.message.text
    if "youtube.com" in link or "youtu.be" in link:
        await handle_youtube_link(update, context, link)
    elif "pinterest.com" in link or "pin.it" in link:
        await handle_pinterest_link(update, context, link)
    else:
        await update.message.reply_text("Жду ссылку на YouTube или Pinterest.")

# --- YOUTUBE С ПОДДЕРЖКОЙ 4K ---

async def handle_youtube_link(update: Update, context: CallbackContext, link: str) -> None:
    try:
        await update.message.reply_text("Ищу форматы... 🧐")
        
        ydl_opts = {'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)

        keyboard = []
        formats = info.get('formats', [])
        
        # Фильтруем уникальные разрешения
        seen_resolutions = set()
        
        # Сортируем от лучшего к худшему
        for f in reversed(formats):
            # Нам нужны видео (даже если без звука, мы их склеим) mp4/webm
            if f.get('vcodec') != 'none' and f.get('height'):
                res = f.get('height')
                if res not in seen_resolutions:
                    filesize = f.get('filesize') or f.get('filesize_approx')
                    size_str = f"{round(filesize / 1024 / 1024)} MB" if filesize else "?"
                    
                    # Формируем кнопку. Передаем resolution, чтобы потом скачать bestvideo[height=X]+bestaudio
                    callback_data = f"yt_{res}"
                    keyboard.append([InlineKeyboardButton(f"🎬 {res}p ({size_str})", callback_data=callback_data)])
                    seen_resolutions.add(res)
        
        keyboard.append([InlineKeyboardButton("🎵 Аудио (MP3)", callback_data="yt_audio")])

        if not keyboard:
            await update.message.reply_text("Не нашел форматов.")
            return
            
        context.user_data['yt_link'] = link
        reply_markup = InlineKeyboardMarkup(keyboard[:8]) # Показываем топ-8 вариантов, чтобы не засорять чат
        await update.message.reply_text(f"**{info.get('title')}**\nВыберите качество:", reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"YouTube Error: {e}")
        await update.message.reply_text("Ошибка при чтении ссылки YouTube.")

async def download_youtube_media(query, context: CallbackContext, data: str):
    try:
        await query.edit_message_text(text="Скачиваю и склеиваю (это может занять время)... ⏳")
        link = context.user_data.get('yt_link')
        action = data.split('_')[1]
        
        output_path = f"downloads/{query.from_user.id}_%(title)s.%(ext)s"
        
        ydl_opts = {
            'outtmpl': output_path,
            'quiet': True,
        }

        if action == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            })
        else:
            # ЛОГИКА 4K: Качаем лучшее видео выбранной высоты + лучшее аудио и склеиваем
            res = action
            ydl_opts.update({
                'format': f'bestvideo[height<={res}]+bestaudio/best[height<={res}]',
                'merge_output_format': 'mp4', # FFmpeg склеит в mp4
            })

        if not os.path.exists('downloads'):
            os.makedirs('downloads')

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=True)
            filename = ydl.prepare_filename(info)
            if action == 'audio':
                filename = os.path.splitext(filename)[0] + ".mp3"

        # Проверка размера перед отправкой (Лимит телеграма для ботов - 50МБ)
        file_size = os.path.getsize(filename)
        if file_size > 49 * 1024 * 1024:
            await query.edit_message_text(text=f"⚠️ Файл слишком большой ({round(file_size/1024/1024)} MB). Telegram запрещает ботам отправлять файлы больше 50 МБ.")
            os.remove(filename)
            return

        await query.edit_message_text(text="Загружаю в Telegram... 🚀")
        
        with open(filename, 'rb') as f:
            if action == 'audio':
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, read_timeout=60, write_timeout=60, connect_timeout=60)
            else:
                await context.bot.send_video(chat_id=query.message.chat_id, video=f, supports_streaming=True, read_timeout=60, write_timeout=60, connect_timeout=60)

        os.remove(filename)
        await query.delete_message()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await query.edit_message_text("Ошибка при скачивании или отправке.")

# --- PINTEREST (JSON method) ---

async def handle_pinterest_link(update: Update, context: CallbackContext, link: str) -> None:
    try:
        await update.message.reply_text("Скачиваю с Pinterest... 📌")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'}
        response = requests.get(link, headers=headers, allow_redirects=True)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        json_data = soup.find('script', {'id': '__PWS_INITIAL_STATE__'})
        if not json_data:
            await update.message.reply_text("Не удалось найти медиа.")
            return

        data = json.loads(json_data.string)
        pin_data = data.get('resourceResponses', [{}])[0].get('response', {}).get('data', {})
        
        # Видео
        if pin_data.get('videos') and pin_data['videos'].get('video_list'):
            # Пытаемся взять лучшее качество
            v_urls = pin_data['videos']['video_list']
            url = v_urls.get('V_720P', {}).get('url') or v_urls.get('V_EXP7', {}).get('url')
            if url:
                await context.bot.send_video(chat_id=update.message.chat_id, video=url)
                return

        # Фото
        image_url = pin_data.get('images', {}).get('orig', {}).get('url')
        if image_url:
            await context.bot.send_photo(chat_id=update.message.chat_id, photo=image_url)
            return

        await update.message.reply_text("Медиа не найдено.")

    except Exception as e:
        logger.error(f"Pinterest Error: {e}")
        await update.message.reply_text("Ошибка при скачивании.")

async def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("yt_"):
        await download_youtube_media(query, context, query.data)

def main() -> None:
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()

    # Запускаем бота
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_handler(CallbackQueryHandler(button))
    application.run_polling()

if __name__ == '__main__':
    main()
