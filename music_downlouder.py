import asyncio
import os
import logging
from typing import List, Dict, Optional, Union
from io import BytesIO
import aiohttp
from dotenv import load_dotenv

from youtube_downloader import YouTubeDownloader
from mp3wr_parser import Mp3wrParser
from sefon_parser import SefonParser

load_dotenv()
logger = logging.getLogger(__name__)


class MusicDownloader:
    """Универсальный класс для поиска и скачивания музыки из разных источников"""
    
    def __init__(self):
        self.youtube = YouTubeDownloader()
        self.sources = ['youtube', 'mp3wr', 'sefon']
        
        # Проверяем доступность YouTube (если есть прокси или работает напрямую)
        self.youtube_available = True
        proxy = os.getenv('PROXY')
        if not proxy:
            logger.warning("YouTube может быть недоступен без прокси в России")
    
    async def search(self, query: str, limit: int = 15) -> List[Dict[str, str]]:
        """
        Поиск музыки по всем доступным источникам
        
        Args:
            query: поисковый запрос
            limit: максимальное количество результатов
            
        Returns:
            Список треков из всех источников
        """
        all_tracks = []
        
        # Пытаемся найти в YouTube (если доступен)
        if self.youtube_available:
            try:
                logger.info(f"Поиск в YouTube: {query}")
                youtube_tracks = await self.youtube.search(query, limit=8)
                
                for track in youtube_tracks:
                    track['source'] = 'youtube'
                    track['source_emoji'] = '📺'
                    all_tracks.append(track)
                    
                logger.info(f"YouTube: найдено {len(youtube_tracks)} треков")
                
            except Exception as e:
                logger.error(f"Ошибка поиска в YouTube: {e}")
                self.youtube_available = False
                logger.warning("YouTube недоступен, используем альтернативные источники")
        
        # Поиск в российских источниках
        try:
            # MP3WR
            logger.info(f"Поиск в MP3WR: {query}")
            async with Mp3wrParser() as mp3wr:
                mp3wr_tracks = await mp3wr.search(query, limit=4)
                
                for track in mp3wr_tracks:
                    track['source'] = 'mp3wr'
                    track['source_emoji'] = '🎵'
                    all_tracks.append(track)
                    
                logger.info(f"MP3WR: найдено {len(mp3wr_tracks)} треков")
                
        except Exception as e:
            logger.error(f"Ошибка поиска в MP3WR: {e}")
        
        try:
            # Sefon
            logger.info(f"Поиск в Sefon: {query}")
            async with SefonParser() as sefon:
                sefon_tracks = await sefon.search(query, limit=3)
                
                for track in sefon_tracks:
                    track['source'] = 'sefon'
                    track['source_emoji'] = '🎶'
                    all_tracks.append(track)
                    
                logger.info(f"Sefon: найдено {len(sefon_tracks)} треков")
                
        except Exception as e:
            logger.error(f"Ошибка поиска в Sefon: {e}")
        
        # Ограничиваем общее количество результатов
        if len(all_tracks) > limit:
            all_tracks = all_tracks[:limit]
        
        logger.info(f"Всего найдено {len(all_tracks)} треков из всех источников")
        return all_tracks
    
    async def download_track(self, track: Dict[str, str]) -> Optional[bytes]:
        """
        Скачивание трека в зависимости от источника
        
        Args:
            track: информация о треке с указанием источника
            
        Returns:
            Байты аудио файла или None
        """
        source = track.get('source', 'youtube')
        
        try:
            if source == 'youtube':
                return await self.youtube.download_track(track['url'])
                
            elif source == 'mp3wr':
                async with Mp3wrParser() as mp3wr:
                    return await mp3wr.download_track(track['url'])
                    
            elif source == 'sefon':
                async with SefonParser() as sefon:
                    return await sefon.download_track(track['track_url'])
            
            else:
                logger.error(f"Неизвестный источник: {source}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка скачивания из {source}: {e}")
            return None
    
    async def test_sources(self) -> Dict[str, bool]:
        """
        Тестирование доступности всех источников
        
        Returns:
            Словарь с результатами тестирования каждого источника
        """
        results = {}
        
        # Тест YouTube
        try:
            test_tracks = await self.youtube.search("test", limit=1)
            results['youtube'] = len(test_tracks) > 0
        except Exception as e:
            logger.error(f"YouTube недоступен: {e}")
            results['youtube'] = False
        
        # Тест MP3WR
        try:
            async with Mp3wrParser() as mp3wr:
                test_tracks = await mp3wr.search("test", limit=1)
                results['mp3wr'] = len(test_tracks) > 0
        except Exception as e:
            logger.error(f"MP3WR недоступен: {e}")
            results['mp3wr'] = False
        
        # Тест Sefon
        try:
            async with SefonParser() as sefon:
                test_tracks = await sefon.search("test", limit=1)
                results['sefon'] = len(test_tracks) > 0
        except Exception as e:
            logger.error(f"Sefon недоступен: {e}")
            results['sefon'] = False
        
        return results