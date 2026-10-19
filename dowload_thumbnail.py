import asyncio
import aiohttp

async def download():
    url = "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0f/Eminem_2021_Color_Corrected.jpg/330px-Eminem_2021_Color_Corrected.jpg"
    
    async with aiohttp.ClientSession(headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }) as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.read()
                with open("thumbnail.jpg", "wb") as f:
                    f.write(data)
                print("✅ Thumbnail скачан!")
            else:
                print(f"❌ Ошибка: {response.status}")

asyncio.run(download())