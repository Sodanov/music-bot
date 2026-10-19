import aiohttp
import re
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from urllib.parse import quote, urljoin


class Mp3wrParser:
    """Класс для работы с mp3wr.com"""
    
    BASE_URL = "https://mp3wr.com"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            }
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def search(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """
        Поиск музыки по запросу
        
        Args:
            query: поисковый запрос
            limit: максимальное количество результатов
            
        Returns:
            Список словарей с информацией о треках
        """
        if not self.session:
            raise RuntimeError("Используйте 'async with Mp3wrParser()' для создания сессии")
        
        # Формируем URL для поиска
        search_url = f"{self.BASE_URL}/search/{quote(query)}"
        
        try:
            async with self.session.get(search_url) as response:
                if response.status != 200:
                    print(f"Статус: {response.status}")
                    return []
                
                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')
                
                results = []
                
                # Ищем блоки с треками - попробуем разные варианты
                # Вариант 1: ищем div с классами track, song, music и т.д.
                track_blocks = soup.find_all(['div', 'li', 'article'], 
                                            class_=re.compile(r'track|song|music|item|result', re.I))
                
                if not track_blocks:
                    # Вариант 2: ищем все ссылки на скачивание
                    track_blocks = soup.find_all('a', href=re.compile(r'/download/|/get/|\.mp3'))
                
                print(f"Найдено блоков: {len(track_blocks)}")
                
                for block in track_blocks[:limit]:
                    try:
                        # Пытаемся извлечь информацию
                        title = "Неизвестно"
                        artist = "Неизвестный исполнитель"
                        download_url = None
                        duration = "N/A"
                        
                        # Ищем название и исполнителя
                        title_elem = block.find(['h2', 'h3', 'h4', 'span', 'div'], 
                                               class_=re.compile(r'title|name', re.I))
                        if title_elem:
                            title = title_elem.get_text(strip=True)
                        
                        artist_elem = block.find(['span', 'div', 'p'], 
                                                class_=re.compile(r'artist|author', re.I))
                        if artist_elem:
                            artist = artist_elem.get_text(strip=True)
                        
                        # Ищем ссылку на скачивание
                        download_link = block.find('a', href=re.compile(r'/download/|/get/|\.mp3'))
                        if not download_link and block.name == 'a':
                            download_link = block
                        
                        if download_link:
                            download_url = urljoin(self.BASE_URL, download_link.get('href', ''))
                        
                        # Если не нашли через классы, пробуем из текста ссылки
                        if title == "Неизвестно" and download_link:
                            link_text = download_link.get_text(strip=True)
                            if link_text and len(link_text) > 3:
                                title = link_text
                        
                        if download_url:
                            results.append({
                                'title': title,
                                'artist': artist,
                                'duration': duration,
                                'download_url': download_url,
                                'full_name': f"{artist} - {title}"
                            })
                        
                    except Exception as e:
                        print(f"Ошибка при парсинге трека: {e}")
                        continue
                
                # Если ничего не нашли стандартными методами, выведем структуру страницы
                if not results:
                    print("\n=== Анализ структуры страницы ===")
                    print(f"Title: {soup.title.string if soup.title else 'Нет'}")
                    
                    # Ищем все ссылки
                    all_links = soup.find_all('a', href=True)
                    print(f"Всего ссылок: {len(all_links)}")
                    
                    mp3_links = [a for a in all_links if 'mp3' in a.get('href', '').lower()]
                    print(f"Ссылок с mp3: {len(mp3_links)}")
                    
                    download_links = [a for a in all_links if any(word in a.get('href', '').lower() 
                                     for word in ['download', 'get', 'track'])]
                    print(f"Ссылок на скачивание: {len(download_links)}")
                    
                    if download_links:
                        print("\nПримеры ссылок:")
                        for link in download_links[:5]:
                            print(f"  - {link.get('href')}: {link.get_text(strip=True)[:50]}")
                
                return results
                
        except Exception as e:
            print(f"Ошибка поиска: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def download_track(self, download_url: str) -> Optional[bytes]:
        """
        Скачивание трека по ссылке
        
        Args:
            download_url: ссылка на скачивание трека
            
        Returns:
            Байты аудио файла или None в случае ошибки
        """
        if not self.session:
            raise RuntimeError("Используйте 'async with Mp3wrParser()' для создания сессии")
        
        try:
            print(f"Скачиваю с: {download_url}")
            
            # Скачиваем файл
            async with self.session.get(download_url, allow_redirects=True) as response:
                print(f"Статус: {response.status}")
                print(f"Content-Type: {response.headers.get('content-type')}")
                
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    
                    # Если это HTML страница, нужно искать реальную ссылку
                    if 'text/html' in content_type:
                        html = await response.text()
                        soup = BeautifulSoup(html, 'lxml')
                        
                        # Ищем прямую ссылку на MP3
                        mp3_link = None
                        
                        # Вариант 1: audio source
                        audio = soup.find('audio')
                        if audio:
                            source = audio.find('source')
                            if source and source.get('src'):
                                mp3_link = source['src']
                        
                        # Вариант 2: кнопка скачивания
                        if not mp3_link:
                            download_btn = soup.find('a', class_=re.compile(r'download', re.I))
                            if download_btn:
                                mp3_link = download_btn.get('href')
                        
                        # Вариант 3: любая ссылка на .mp3
                        if not mp3_link:
                            for link in soup.find_all('a', href=True):
                                if link['href'].endswith('.mp3'):
                                    mp3_link = link['href']
                                    break
                        
                        if mp3_link:
                            if not mp3_link.startswith('http'):
                                mp3_link = urljoin(self.BASE_URL, mp3_link)
                            
                            print(f"Найдена прямая ссылка: {mp3_link}")
                            # Скачиваем по прямой ссылке
                            async with self.session.get(mp3_link, allow_redirects=True) as mp3_response:
                                if mp3_response.status == 200:
                                    return await mp3_response.read()
                        
                        return None
                    
                    # Если это аудио файл
                    elif 'audio' in content_type or 'octet-stream' in content_type:
                        return await response.read()
                    
                return None
                    
        except Exception as e:
            print(f"Ошибка скачивания: {e}")
            import traceback
            traceback.print_exc()
            return None