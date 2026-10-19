import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")
# Необязательно: бесплатные ключи Openverse повышают лимиты запросов
OPENVERSE_CLIENT_ID = os.getenv("OPENVERSE_CLIENT_ID", "")
OPENVERSE_CLIENT_SECRET = os.getenv("OPENVERSE_CLIENT_SECRET", "")

PAGE_SIZE = 12                      # треков на странице выдачи
SEARCH_LIMIT_PER_SOURCE = 10       # сколько брать из каждого источника
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # лимит Telegram на файл от бота
USER_COOLDOWN_SEC = 3              # защита от спама запросами
