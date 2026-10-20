"""
Модуль для поиска и скачивания музыки с YouTube и SoundCloud через yt-dlp.

Настройки через переменные окружения (все необязательные):
    PROXY         - прокси, например http://user:pass@host:port или socks5://host:port
    COOKIES_FILE  - путь к cookies.txt (формат Netscape), помогает при "Sign in to confirm you're not a bot"
"""
import asyncio
import glob
import logging
import os
import shutil
import tempfile
import time
from typing import Dict, List, Optional

import yt_dlp

logger = logging.getLogger(__name__)

ydl_logger = logging.getLogger("yt-dlp")
ydl_logger.setLevel(logging.INFO)

# Лимит Telegram для ботов на отправку файла - 50 МБ, оставляем запас
MAX_FILE_SIZE = 49 * 1024 * 1024

# Клиенты YouTube для повторных попыток. None = выбор yt-dlp по умолчанию.
# Поддержка клиентов в yt-dlp меняется, поэтому держите yt-dlp свежим:
#     pip install -U yt-dlp
PLAYER_CLIENTS = [None, ['mweb'], ['tv']]


class YouTubeDownloader:
    """Класс для работы с YouTube и SoundCloud через yt-dlp"""

    def __init__(self):
        self.proxy = os.getenv('PROXY') or None
        self.cookies_file = None
        cookies_src = os.getenv('COOKIES_FILE') or None
        if cookies_src and os.path.exists(cookies_src):
            cookies_dst = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')
            shutil.copyfile(cookies_src, cookies_dst)
            self.cookies_file = cookies_dst
        elif cookies_src:
            logger.warning(f"COOKIES_FILE указан, но файл не найден: {cookies_src}")
        self.has_ffmpeg = shutil.which('ffmpeg') is not None

        if not self.has_ffmpeg:
            logger.warning(
                "ffmpeg не найден: аудио не будет конвертироваться в mp3 "
                "(на Mac: brew install ffmpeg)"
            )

        logger.info(
            f"YouTubeDownloader: прокси={'да' if self.proxy else 'нет'}, "
            f"cookies={'да' if self.cookies_file else 'нет'}, "
            f"ffmpeg={'да' if self.has_ffmpeg else 'нет'}"
        )
        logger.info("yt-dlp version: %s", yt_dlp.version.__version__)

    # ------------------------------------------------------------------ опции

    def _common_opts(self) -> dict:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 30,
            'retries': 3,
        }
        if self.proxy:
            opts['proxy'] = self.proxy
        if self.cookies_file:
            opts['cookiefile'] = self.cookies_file
        return opts

    def _search_opts(self) -> dict:
        opts = self._common_opts()
        opts['extract_flat'] = True  # только метаданные, без скачивания
        return opts

    def _download_opts(self, outdir: str, player_clients: Optional[List[str]]) -> dict:
        opts = self._common_opts()
        opts.update({
            # Лучшее аудио, но не больше ~45 МБ (запас под лимит Telegram)
            'format': 'bestaudio[filesize<45M]/bestaudio[filesize_approx<45M]/bestaudio/best',
            'outtmpl': os.path.join(outdir, '%(id)s.%(ext)s'),
            'socket_timeout': 60,
            'http_chunk_size': 1048576,
        })
        if self.has_ffmpeg:
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        if player_clients:
            opts['extractor_args'] = {'youtube': {'player_client': player_clients}}
        return opts

    # ------------------------------------------------------------------ поиск

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """
        Поиск музыки на YouTube.

        Returns:
            Список словарей: title, artist, duration, url, full_name
        """
        try:
            search_query = f"ytsearch{limit}:{query} audio"
            logger.info(f"Выполняем поиск: {search_query}")

            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(None, self._search_sync, search_query)

            if not results or not results.get('entries'):
                logger.warning(f"YouTube не вернул результаты для запроса: {query}")
                return []

            tracks = []
            for entry in results['entries']:
                if not entry:
                    continue

                video_id = entry.get('id')
                title = entry.get('title') or 'Неизвестно'
                uploader = entry.get('uploader') or entry.get('channel') or 'Неизвестный исполнитель'
                url = entry.get('url') or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
                if not url:
                    continue

                tracks.append({
                    'title': title,
                    'artist': uploader,
                    'duration': self._format_duration(entry.get('duration')),
                    'duration_sec': int(entry.get('duration') or 0),
                    'url': url,
                    'full_name': f"{uploader} - {title}",
                })

            logger.info(f"Поиск вернул {len(tracks)} треков для запроса: {query}")
            return tracks

        except Exception as e:
            logger.error(f"Ошибка поиска: {e}", exc_info=True)
            return []

    def _search_sync(self, search_query: str) -> dict:
        with yt_dlp.YoutubeDL(self._search_opts()) as ydl:
            return ydl.extract_info(search_query, download=False)

    async def search_soundcloud(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """Поиск на SoundCloud. Без прокси и без YouTube-cookies."""
        try:
            search_query = f"scsearch{limit}:{query}"
            logger.info(f"Выполняем поиск: {search_query}")

            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(None, self._search_sc_sync, search_query)

            tracks = []
            for entry in (results or {}).get('entries', []):
                if not entry or not entry.get('url'):
                    continue
                tracks.append({
                    'title': entry.get('title') or 'Неизвестно',
                    'artist': entry.get('uploader') or entry.get('channel') or 'Неизвестный исполнитель',
                    'duration_sec': int(entry.get('duration') or 0),
                    'url': entry['url'],
                })

            logger.info(f"SoundCloud вернул {len(tracks)} треков для запроса: {query}")
            return tracks

        except Exception as e:
            logger.error(f"Ошибка поиска на SoundCloud: {e}", exc_info=True)
            return []

    def _search_sc_sync(self, search_query: str) -> dict:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'socket_timeout': 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(search_query, download=False)

    # ------------------------------------------------------------- скачивание

    async def download_track(self, url: str) -> Optional[bytes]:
        """
        Скачивает трек и возвращает байты аудио (mp3, если установлен ffmpeg).
        None - если не удалось или файл больше лимита Telegram.
        """
        try:
            logger.info(f"Начало скачивания: {url}")
            loop = asyncio.get_running_loop()
            audio_data = await loop.run_in_executor(None, self._download_sync, url)

            if not audio_data:
                return None

            logger.info(f"Файл получен: {len(audio_data) / 1024 / 1024:.2f} МБ")

            if len(audio_data) > MAX_FILE_SIZE:
                logger.warning("Файл больше лимита Telegram (50 МБ), отправка невозможна")
                return None

            return audio_data

        except Exception as e:
            logger.error(f"Ошибка скачивания: {e}", exc_info=True)
            return None

    def _download_sync(self, url: str) -> Optional[bytes]:
        """Скачивание во временную папку. Для YouTube - с повторами через разные клиенты."""
        is_yt = 'youtube.com' in url or 'youtu.be' in url
        clients_list = PLAYER_CLIENTS if is_yt else [None]

        for attempt, clients in enumerate(clients_list, start=1):
            # Отдельная временная папка на каждую попытку и каждого пользователя:
            # одновременные загрузки не мешают друг другу, мусор удаляется сам
            with tempfile.TemporaryDirectory(prefix='ytdl_') as tmpdir:
                try:
                    logger.info(
                        f"Попытка {attempt}/{len(clients_list)}: клиент={clients or 'по умолчанию'}"
                    )
                    opts = self._download_opts(tmpdir, clients)
                    if not is_yt:
                        opts.pop('proxy', None)       # платный прокси не тратим на SoundCloud
                        opts.pop('cookiefile', None)  # YouTube-cookies здесь не нужны
                    opts["logger"] = ydl_logger

                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.extract_info(url, download=True)
                    files = [
                        f for f in glob.glob(os.path.join(tmpdir, '*'))
                        if not f.endswith(('.part', '.ytdl'))
                    ]
                    if not files:
                        logger.warning("Файл не найден после скачивания")
                    else:
                        with open(files[0], 'rb') as f:
                            return f.read()

                except Exception as e:
                    msg = str(e)
                    logger.error(f"Ошибка при попытке {attempt}: {msg}")

                    low = msg.lower()
                    if 'video unavailable' in low or 'private video' in low:
                        logger.error("Видео недоступно или приватное - повторы бессмысленны")
                        return None
                    if 'sign in to confirm' in low or 'not a bot' in low:
                        logger.warning(
                            "YouTube просит подтвердить, что вы не бот. "
                            "Помогает COOKIES_FILE и/или прокси с другим IP."
                        )

            if attempt < len(clients_list):
                time.sleep(1 + attempt)

        logger.error("Все попытки скачивания исчерпаны")
        return None

    # ---------------------------------------------------------------- утилиты

    @staticmethod
    def _format_duration(seconds) -> str:
        """Длительность в формате минуты:секунды"""
        if not seconds:
            return "N/A"
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"
