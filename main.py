import os
import re
import json
import base64
import asyncio
import urllib.parse
import io  
import datetime
import time
import string
import random
import subprocess
import shutil
import discord
import aiohttp
import yt_dlp
from discord.ext import commands
from discord import app_commands

# Tự động nạp thư viện Opus âm thanh cho Linux / Railway
if not discord.opus.is_loaded():
    for lib_name in ['libopus.so.0', 'libopus.so', 'libopus-0.dll', 'libopus.dylib']:
        try:
            discord.opus.load_opus(lib_name)
            print(f"[+] Đã nạp thành công Opus library: {lib_name}")
            break
        except Exception:
            pass

# Xử lý import tương thích cho DuckDuckGo Search
try:
    from duckduckgo_search import DDGS
except ImportError:
    from ddgs import DDGS  

from cerebras.cloud.sdk import AsyncCerebras

# Tự động lấy file thực thi FFmpeg từ hệ thống hoặc imageio-ffmpeg
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        FFMPEG_PATH = "ffmpeg"

# ==================== CẤU HÌNH HỆ THỐNG ====================
from dotenv import load_dotenv
load_dotenv()

# Hàm hỗ trợ đọc file JSON
def load_json(filepath, default_value):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_value

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Lỗi lưu file {filepath}: {e}")

# TỰ ĐỘNG NẠP CẤU HÌNH TỪ CONFIG.PY HOẶC FALLBACK SANG .ENV / CONFIG.JSON
try:
    import config as sys_config
    BOT_TOKEN = getattr(sys_config, 'BOT_TOKEN', os.getenv('TOKEN'))
    CEREBRAS_API_KEY = getattr(sys_config, 'CEREBRAS_API_KEY', os.getenv('CEREBRAS_API_KEY'))
    OWNER_IDS = getattr(sys_config, 'OWNER_IDS', [739495906956673095, 987654321098765432])
    GUILD_IDS = getattr(sys_config, 'GUILD_IDS', load_json("config.json", {}).get("guild_ids", []))
    MODEL_NAME = getattr(sys_config, 'MODEL_NAME', "gemma-4-31b")
except ImportError:
    BOT_TOKEN = os.getenv('TOKEN')
    CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
    OWNER_IDS = [739495906956673095, 987654321098765432]
    config_json = load_json("config.json", {"guild_ids": []})
    GUILD_IDS = config_json.get("guild_ids", [])
    MODEL_NAME = "gemma-4-31b"

MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"

if not BOT_TOKEN or not CEREBRAS_API_KEY:
    print("LỖI: Thiếu BOT_TOKEN hoặc CEREBRAS_API_KEY trong file config.py hoặc .env!")
    exit(1)

client = AsyncCerebras(api_key=CEREBRAS_API_KEY)
http_session = None

async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session

# ==================== CẤU HÌNH PHÁT NHẠC & LIVESTREAM (TỐI ƯU RAILWAY) ====================
YTDL_BASE_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,  
    'nocheckcertificate': True,
    'ignoreerrors': True, 
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
    'extractor_args': {'youtube': ['player_client=ios,web']},
    'socket_timeout': 15,
}

def get_ytdl_opts(custom_opts=None):
    """Hàm tạo cấu hình YTDL tự động tích hợp cookies.txt nếu file tồn tại"""
    opts = YTDL_BASE_OPTIONS.copy()
    if custom_opts:
        opts.update(custom_opts)
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
    return opts

# Cấu hình tối ưu riêng cho Railway (Bỏ video, giảm dung lượng buffer chống tràn RAM gây lỗi -9)
FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        '-probesize 32768 -analyzeduration 0 '
        '-user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'
    ),
    'options': '-vn -sn -dn -filter:a "volume=1.0"'
}

music_queues = {} 
loop_mode = {}       
current_playing = {} 

# Quản lý tiến trình Livestream RTMP
active_streams = {}
RTMP_SERVERS = {
    "YouTube": "rtmp://a.rtmp.youtube.com/live2/",
    "Facebook": "rtmps://live-api-s.facebook.com:443/rtmp/",
    "TikTok": "Tùy chỉnh (Nhập RTMP Server URL thủ công)"
}

memory = load_json(MEMORY_FILE, {})
user_chat_sessions = {}
VALID_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
VALID_TEXT_EXTS = {'.txt', '.json', '.py', '.js', '.html', '.css', '.md', '.log', '.csv', '.yaml', '.yml', '.ini', '.conf'}

# ==================== CÔNG CỤ TÌM KIẾM AI ====================
async def fetch_realtime_news(query):
    try:
        results = await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=5)))
        return results
    except Exception: return []

async def fetch_images(query):
    try:
        results = await asyncio.to_thread(lambda: list(DDGS().images(query, max_results=3)))
        return [r['image'] for r in results if 'image' in r]
    except Exception: return []

async def search_soundcloud(keyword, max_results=15):
    """Hàm tìm kiếm bài hát CHỈ TRÊN SOUNDCLOUD bằng yt_dlp"""
    extract_opts = get_ytdl_opts({'extract_flat': True})
    
    search_query = f"scsearch{max_results}:{keyword}"
    try:
        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, search_query, download=False)
            results = []
            if info and 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        url = entry.get('url') or entry.get('webpage_url')
                        title = entry.get('title', 'Không rõ tên bài')
                        clean_title = title.replace(' | Stream Free Listening on SoundCloud', '')
                        results.append({
                            'title': clean_title,
                            'url': url,
                            'webpage_url': url
                        })
            return results
    except Exception as e:
        print(f"Lỗi tìm kiếm SoundCloud: {e}")
        return []

# ==================== TRÍ NHỚ TỰ ĐỘNG ====================
async def auto_extract_memory(user_id, user_name, user_msg, bot_reply):
    user_id_str = str(user_id)
    try:
        existing_facts = memory.get(user_id_str, [])
        existing_facts_str = "\n".join([f"- {f}" for f in existing_facts]) if existing_facts else "Chưa có ký ức nào."

        extraction_prompt = [
            {"role": "system", "content": (
                f"Bạn là hệ thống phân tích trí nhớ. Trích xuất thông tin cá nhân cốt lõi của người dùng '{user_name}'.\n"
                f"Ký ức hiện tại:\n{existing_facts_str}\n\n"
                "QUY TẮC:\n"
                "1. Chỉ trích xuất thông tin mới về NGƯỜI DÙNG.\n"
                "2. Nếu không có thông tin mới, chỉ trả lời: NONE.\n"
                "3. Viết dưới dạng gạch đầu dòng ngắn gọn."
            )},
            {"role": "user", "content": f"Người dùng nói: {user_msg}\nBot trả lời: {bot_reply}"}
        ]

        response = await client.chat.completions.create(
            model=MODEL_NAME, messages=extraction_prompt, temperature=0.6, max_completion_tokens=150
        )
        result = response.choices[0].message.content.strip()
        
        if result and result.upper() != "NONE" and "none" not in result.lower():
            new_facts = [line.strip("- ").strip() for line in result.split("\n") if line.strip()]
            if user_id_str not in memory: memory[user_id_str] = []
                
            updated = False
            for fact in new_facts:
                if fact not in memory[user_id_str] and len(fact) > 2:
                    memory[user_id_str].append(fact)
                    updated = True
                    
            if updated:
                memory[user_id_str] = memory[user_id_str][-20:]
                await asyncio.to_thread(save_json, MEMORY_FILE, memory)
    except Exception: pass

# ==================== XỬ LÝ MEDIA ====================
async def get_base64_image(url):
    session = await get_http_session()
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                image_data = await response.read()
                mime_type = response.headers.get('Content-Type', 'image/jpeg')
                return f"data:{mime_type};base64,{base64.b64encode(image_data).decode('utf-8')}"
    except Exception: return None
    
def get_fb_video_url(url):
    ydl_opts = get_ytdl_opts({'format': 'best', 'quiet': True, 'no_warnings': True})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try: return ydl.extract_info(url, download=False).get('url')
        except: return None

def get_live_audio_url(tiktok_url):
    ydl_opts = get_ytdl_opts({'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try: return ydl.extract_info(tiktok_url, download=False).get('url')
        except: return None

async def extract_video_context(url):
    session = await get_http_session()
    if "youtube.com" in url or "youtu.be" in url:
        encoded_url = urllib.parse.quote(url, safe='')
        try:
            async with session.get(f"https://www.youtube.com/oembed?url={encoded_url}&format=json", timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return f"[BỐI CẢNH VIDEO YOUTUBE] Tiêu đề: {data.get('title')} | Kênh: {data.get('author_name')}"
        except: pass
    
    try: 
        ydl_opts = get_ytdl_opts({'skip_download': True, 'quiet': True, 'extract_flat': True})
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            return f"[BỐI CẢNH VIDEO] Tiêu đề: {info.get('title', 'Không rõ')}"
    except: 
        return "[VIDEO] Không đọc được thông tin."

# ==================== LOGIC PHÁT NHẠC ====================
async def play_stream_async(guild_id, voice_client, song_info):
    raw_url = song_info.get('url')
    if not raw_url or not voice_client or not voice_client.is_connected():
        play_next(guild_id, voice_client)
        return

    stream_url = None
    try:
        ydl_opts = get_ytdl_opts({'format': 'bestaudio/best', 'quiet': True})
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, raw_url, download=False)
            if info:
                stream_url = info.get('url')
                if not stream_url and 'formats' in info:
                    audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                    if audio_formats:
                        stream_url = audio_formats[-1].get('url')
    except Exception as e:
        print(f"[!] Lỗi trích xuất URL nhạc: {e}")

    if not stream_url:
        stream_url = raw_url

    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        source = discord.FFmpegPCMAudio(
            stream_url, 
            executable=FFMPEG_PATH, 
            **FFMPEG_OPTIONS
        )

        def after_callback(error):
            if error:
                print(f"[!] Lỗi FFmpeg Playback: {error}")
            play_next(guild_id, voice_client)

        voice_client.play(source, after=after_callback)
        print(f"[+] Đang phát thành công: {song_info.get('title')}")

    except Exception as e:
        print(f"[!] Lỗi khởi tạo FFmpeg: {e}")
        play_next(guild_id, voice_client)

def play_next(guild_id, voice_client):
    if not voice_client or not voice_client.is_connected():
        current_playing.pop(guild_id, None)
        return

    mode = loop_mode.get(guild_id, "OFF")
    last_song = current_playing.get(guild_id)

    if mode == "QUEUE" and last_song:
        music_queues.setdefault(guild_id, []).append(last_song)

    if mode == "SONG" and last_song:
        next_song = last_song
    else:
        if guild_id in music_queues and len(music_queues[guild_id]) > 0:
            next_song = music_queues[guild_id].pop(0)
        else:
            current_playing.pop(guild_id, None)
            return

    current_playing[guild_id] = next_song
    asyncio.run_coroutine_threadsafe(play_stream_async(guild_id, voice_client, next_song), bot.loop)

async def extract_audio_info(search_or_url):
    extract_opts = get_ytdl_opts({'extract_flat': 'in_playlist'})
    
    try:
        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, search_or_url, download=False)
            playlist = []
            if info and 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        url = entry.get('webpage_url') or entry.get('url')
                        playlist.append((url, entry.get('title', 'Không rõ tên bài')))
            elif info:
                url = info.get('webpage_url') or info.get('url')
                playlist.append((url, info.get('title', 'Không rõ tên bài')))
            return playlist
    except Exception as e:
        print(f"Lỗi trích xuất Audio: {e}")
        return []

# ==================== GIAO DIỆN SELECT MENU CHO NHẠC ====================
class SongSelectView(discord.ui.View):
    def __init__(self, all_results, guild_id):
        super().__init__(timeout=120)
        self.all_results = all_results
        self.guild_id = guild_id
        self.page = 0
        self.per_page = 5
        self.update_select()

    def update_select(self):
        self.clear_items()
        start = self.page * self.per_page
        end = start + self.per_page
        current_results = self.all_results[start:end]

        select = discord.ui.Select(
            placeholder=f"Trang {self.page + 1}: Chọn bài đi đồ ngốc!",
            options=[discord.SelectOption(label=f"{i+1}. {r['title'][:90]}", value=str(start + i)) for i, r in enumerate(current_results) if r]
        )
        select.callback = self.select_callback
        self.add_item(select)

        if self.page > 0:
            prev_btn = discord.ui.Button(label="⬅ Trang trước", style=discord.ButtonStyle.secondary)
            prev_btn.callback = self.prev_callback
            self.add_item(prev_btn)

        if end < len(self.all_results):
            next_btn = discord.ui.Button(label="Trang sau ➡", style=discord.ButtonStyle.primary)
            next_btn.callback = self.next_callback
            self.add_item(next_btn)

        search_again_btn = discord.ui.Button(label="Tìm từ khóa khác", style=discord.ButtonStyle.danger)
        search_again_btn.callback = self.search_again_callback
        self.add_item(search_again_btn)

    async def select_callback(self, interaction: discord.Interaction):
        index = int(interaction.data['values'][0])
        selected_song = self.all_results[index]
        song_url = selected_song.get('webpage_url') or selected_song.get('url') 
        
        voice_client = interaction.guild.voice_client
        if not voice_client:
            await interaction.response.edit_message(content="Bot đã bị ngắt kết nối khỏi kênh thoại!", view=None)
            return

        music_queues.setdefault(self.guild_id, []).append({'url': song_url, 'title': selected_song['title']})
        
        if not voice_client.is_playing() and not voice_client.is_paused():
            song = music_queues[self.guild_id].pop(0)
            current_playing[self.guild_id] = song
            await play_stream_async(self.guild_id, voice_client, song)
            await interaction.response.edit_message(content=f"Được rồi! Kê sẽ hát: **{song['title']}**. Nghe cho thủng màng nhĩ đi! 😳", view=None)
        else:
            await interaction.response.edit_message(content=f"📌 Đã đẩy **{selected_song['title']}** ra đằng sau hàng chờ! Đợi đi đồ ngốc!", view=None)

    async def prev_callback(self, interaction: discord.Interaction):
        self.page -= 1; self.update_select(); await interaction.response.edit_message(view=self)
    async def next_callback(self, interaction: discord.Interaction):
        self.page += 1; self.update_select(); await interaction.response.edit_message(view=self)
    async def search_again_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self.guild_id))

class SearchModal(discord.ui.Modal, title="Tìm bài hát mới trên SoundCloud"):
    keyword = discord.ui.TextInput(label="Nhập tên bài hát SoundCloud:", placeholder="Ví dụ: Lofi chill, remix...", required=True)
    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id
        
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bai_hat = self.keyword.value
        
        results = await search_soundcloud(bai_hat)
        
        if not results:
            await interaction.followup.send("Hứ! Tìm trên SoundCloud không ra bài nào hết! Đổi từ khóa khác đi! 💢", ephemeral=True)
            return
            
        new_view = SongSelectView(results, self.guild_id)
        await interaction.followup.send(f"Đây là kết quả SoundCloud mới cho: **{bai_hat}**", view=new_view)

def chunk_text(text, limit=1900):
    return [text[i:i+limit] for i in range(0, len(text), limit)]

# ==================== KHỞI TẠO DISCORD BOT ====================
intents = discord.Intents.default()
intents.message_content = True 
intents.voice_states = True 

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await get_http_session()
    
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="Đang sục cặc"))
    print(f'=== Kê đã sẵn sàng cãi lộn ({bot.user}) ===')
    
    allowed_guilds = set()
    for g in GUILD_IDS:
        try:
            allowed_guilds.add(int(g))
        except (ValueError, TypeError):
            pass

    for guild in bot.guilds:
        if allowed_guilds and guild.id not in allowed_guilds:
            try: await guild.leave()
            except: pass
            
    if allowed_guilds:
        for g_id in allowed_guilds:
            try:
                guild_obj = discord.Object(id=g_id)
                bot.tree.copy_global_to(guild=guild_obj)
                await bot.tree.sync(guild=guild_obj)
            except Exception as e:
                print(f"Lỗi Sync Lệnh Guild {g_id}: {e}")
    else:
        await bot.tree.sync()

@bot.event
async def on_message(message):
    if message.author.bot: return

    if bot.user in message.mentions:
        prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        image_url, video_target_url, file_context = None, None, ""
        
        if message.attachments:
            for attachment in message.attachments:
                ext = os.path.splitext(attachment.filename)[1].lower()
                if ext in VALID_IMAGE_EXTS and not image_url:
                    image_url = attachment.url
                elif ext in VALID_TEXT_EXTS or attachment.size < 500000:
                    try:
                        file_content = (await attachment.read()).decode('utf-8', errors='ignore')
                        if len(file_content) > 10000: 
                            file_content = file_content[:10000] + "\n...[Cắt bớt]..."
                        file_context += f"\n[FILE {attachment.filename}]:\n```\n{file_content}\n```\n"
                    except: pass

        link_match = re.search(r'(https?://[^\s]+)', prompt)
        if link_match:
            url = link_match.group(1)
            if any(domain in url.lower() for domain in ['youtube.com', 'youtu.be', 'tiktok.com', 'facebook.com', 'fb.watch', 'instagram.com']):
                video_target_url = url

        if not prompt and not image_url and not video_target_url and not file_context:
            await message.reply("Kêu ta làm gì? Bộ rảnh lắm hả đồ ngốc? 💢")
            return

        async with message.channel.typing():
            try:
                video_context = await extract_video_context(video_target_url) if video_target_url else ""
                
                user_id_str = str(message.author.id)
                user_chat_sessions.setdefault(user_id_str, [])
                history_for_ai = user_chat_sessions[user_id_str]

                web_context = ""
                search_triggers = {'hôm nay', 'bây giờ', 'mới nhất', 'hiện tại', 'giá', 'tin tức', 'thời tiết', 'tìm link', 'có link', '2024', '2025', '2026'}
                if any(trigger in prompt.lower() for trigger in search_triggers):
                    web_results = await fetch_realtime_news(prompt)
                    if web_results:
                        web_context = "\n[THÔNG TIN TRÊN INTERNET]:\n"
                        for r in web_results:
                            web_context += f"- {r.get('title', '')}\n  Nội dung: {r.get('body', '')}\n  Link Nguồn: {r.get('href', '')}\n"

                found_image_urls = []
                image_triggers = {'tìm ảnh', 'ảnh của', 'hình của', 'hình ảnh', 'gửi ảnh'}
                if any(trigger in prompt.lower() for trigger in image_triggers):
                    found_image_urls = await fetch_images(prompt)

                current_time = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

                system_prompt = {
                    "role": "system", 
                    "content": (f"Bạn là Kê. Ký ức về {message.author.display_name}:\n{memory.get(user_id_str, [])}\n"
                                f"Hôm nay là {current_time}. "
                                "Quy tắc: LUÔN trích dẫn 'Link Nguồn' (URL) nếu có. "
                                "NẾU TẠO FILE, bọc toàn bộ nội dung file vào thẻ [FILE: ten_file.ext] và [/FILE].\n"
                                "Bạn là tên Kê, một phiên bản thứ 2 của Kê Lão Đại. Thông minh, hài hước, am hiểu mọi thứ. "
                                "Cá tính: tự nhiên, dịu dàng nhưng CÓ CHÚT TSUNDERE: ngoài lạnh trong nóng. "
                                "Hay nói 'Đồ ngốc', 'Đừng hiểu lầm', gắt gỏng khi bị trêu nhưng vẫn giúp. Dùng nhiều icon (💢, 😳, 😤, 🙄). "
                                "Thích làm thơ, lâu lâu chèn một đoạn thơ có vần hay. Dài ngắn tùy ngữ cảnh.")
                }
                
                parts = [ctx for ctx in (web_context, video_context, file_context, f"[CÂU HỎI]: {prompt}") if ctx]
                user_text = "\n".join(parts).strip()
                content = user_text
                
                if image_url:
                    b64 = await get_base64_image(image_url)
                    if b64: content = [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": b64}}]

                reply_text, api_success = "Đã xảy ra lỗi không xác định.", False
                for attempt in range(3):
                    try:
                        response = await client.chat.completions.create(
                            model=MODEL_NAME, messages=[system_prompt] + history_for_ai + [{"role": "user", "content": content}], temperature=0.85, max_completion_tokens=2000
                        )
                        reply_text = response.choices[0].message.content
                        api_success = True; break
                    except Exception as e:
                        if "429" in str(e) and attempt < 2:
                            await asyncio.sleep(2); continue
                        reply_text = f"Mạng nhà Kê bị chập mạch rồi đồ ngốc: `{e}`"
                
                files_to_send = []
                file_pattern = r'\[FILE:\s*([^\]]+)\](.*?)\[/FILE\]'
                for match in re.finditer(file_pattern, reply_text, re.DOTALL):
                    filename = match.group(1).strip()
                    file_content = match.group(2).strip()
                    files_to_send.append(discord.File(io.BytesIO(file_content.encode('utf-8')), filename=filename))
                
                clean_reply = re.sub(file_pattern, '', reply_text, flags=re.DOTALL).strip()

                if clean_reply:
                    chunks = chunk_text(clean_reply)
                    for i, chunk in enumerate(chunks):
                        files = files_to_send if i == 0 else None
                        if len(chunks) == 1:
                            await message.reply(embed=discord.Embed(description=chunk, color=0x3498db), files=files or [])
                        else:
                            await message.reply(chunk, files=files or []) if i == 0 else await message.channel.send(chunk)
                elif files_to_send:
                    await message.reply("Cầm lấy file này đi! Kê không phải người hầu của ngươi đâu đấy! 😤", files=files_to_send)
                
                for img in found_image_urls: await message.channel.send(img)

                if api_success: 
                    user_chat_sessions[user_id_str].append({"role": "user", "content": prompt[:800] + ("..." if len(prompt)>800 else "")})
                    user_chat_sessions[user_id_str].append({"role": "assistant", "content": reply_text[:800] + ("..." if len(reply_text)>800 else "")})
                    user_chat_sessions[user_id_str] = user_chat_sessions[user_id_str][-8:] 
                    asyncio.create_task(auto_extract_memory(message.author.id, message.author.display_name, prompt, reply_text))
            except Exception as e: await message.reply(f"Hệ thống Kê bị hỏng rồi: {e} 💢")
    await bot.process_commands(message)

# ==================== SLASH COMMANDS - TIỆN ÍCH KHÁC ====================
@bot.tree.command(name="sync", description="Đồng bộ lệnh Slash Commands ngay lập tức cho server này")
@app_commands.checks.has_permissions(administrator=True)
async def sync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.copy_global_to(guild=interaction.guild)
        await bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send("Đã đồng bộ lệnh thành công! Thử tải lại (Ctrl+R) Discord đi đồ ngốc. 😤", ephemeral=True)
    except Exception as e: await interaction.followup.send(f"Lỗi: {e}", ephemeral=True)

@bot.tree.command(name="tiktok", description="Chuyển đổi link TikTok sang link tải trực tiếp")
@app_commands.describe(link="Đường link bài viết hoặc video TikTok")
async def tiktok_convert(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    session = await get_http_session()
    try:
        async with session.head(link, allow_redirects=True, timeout=10) as resp:
            match = re.search(r"(@[^\/]+)/(?:video|photo)/(\d+)", str(resp.url))
            if match: await interaction.followup.send(f"Của ngươi đây: https://d.tiktokez.com/{match.group(1)}/video/{match.group(2)} 🙄")
            else: await interaction.followup.send("Link cái kiểu gì thế này? Lỗi rồi! 💢")
    except Exception as e: await interaction.followup.send(f"Mạng mẽo chán quá: {e}")

@bot.tree.command(name="fb", description="Chuyển đổi link video Facebook")
@app_commands.describe(link="Đường link video Facebook cần trích xuất")
async def fb_convert(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    try:
        video_url = await asyncio.to_thread(get_fb_video_url, link)
        if video_url: await interaction.followup.send(f"Lấy đi: {video_url} 😤")
        else: await interaction.followup.send("Không moi được video từ link này đâu đồ ngốc!")
    except Exception as e: await interaction.followup.send(f"Lỗi: {e}")

@bot.tree.command(name="nuke", description="Xóa hàng loạt tin nhắn trong kênh hiện tại")
@app_commands.describe(limit="Số lượng tin nhắn cần xóa (Mặc định: 10000)")
@app_commands.checks.has_permissions(manage_channels=True)
async def nuke(interaction: discord.Interaction, limit: int = 10000):
    await interaction.response.defer()
    try:
        deleted = await interaction.channel.purge(limit=limit)
        await interaction.followup.send(f"Đã dọn sạch {len(deleted)} tin rác rưởi. Đừng xả rác nữa!")
    except Exception as e: await interaction.followup.send(f"Quét mỏi tay quá bị lỗi rồi: {e}")

@bot.tree.command(name="clearbot", description="Xóa các tin nhắn do chính Bot gửi")
@app_commands.describe(amount="Số lượng tin nhắn cần quét (Mặc định: 50)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clearbot(interaction: discord.Interaction, amount: int = 50):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.author == bot.user)
        await interaction.followup.send(f"Đã xóa {len(deleted)} tin của Kê rồi. Vừa lòng chưa?! 😤", ephemeral=True)
    except Exception as e: await interaction.followup.send(f"Lỗi dọn tin: {e}", ephemeral=True)

# ==================== SLASH COMMANDS - PHÁT NHẠC ====================
@bot.tree.command(name="play", description="Tìm kiếm bài hát trên SoundCloud và chọn bài ưa thích")
@app_commands.describe(bai_hat="Tên bài hát trên SoundCloud hoặc link nhạc trực tiếp")
async def play(interaction: discord.Interaction, bai_hat: str):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Đồ ngốc! Không vào kênh thoại thì Kê hát cho ma nghe à? 💢")
        return
    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    if not voice_client: 
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel: 
        await voice_client.move_to(voice_channel)
        
    guild_id = interaction.guild.id
    music_queues.setdefault(guild_id, [])

    try:
        if "http://" in bai_hat or "https://" in bai_hat:
            if ".m3u8" in bai_hat or ".flv" in bai_hat: 
                playlist = [(bai_hat, "Livestream (Trực tiếp)")]
            elif "tiktok.com" in bai_hat and "live" in bai_hat.lower():
                stream_url = await asyncio.to_thread(get_live_audio_url, bai_hat)
                if not stream_url:
                    await interaction.followup.send("Kê không lấy được Live này! 💢")
                    return
                playlist = [(stream_url, "TikTok Livestream Audio")]
            else: 
                playlist = await extract_audio_info(bai_hat)

            if not playlist:
                await interaction.followup.send("🙄 Gửi link vớ vẩn gì thế đồ ngốc?! 💢")
                return
            for url, title in playlist: 
                music_queues[guild_id].append({'url': url, 'title': title})
            
            if not voice_client.is_playing() and not voice_client.is_paused():
                first_song = music_queues[guild_id].pop(0)
                current_playing[guild_id] = first_song
                await play_stream_async(guild_id, voice_client, first_song)
                await interaction.followup.send(f"Kê bắt đầu phát: **{first_song['title']}**. Cấm chê! 😳")
            else: 
                await interaction.followup.send("Đã nhét vào hàng chờ rồi!")
            return

        results = await search_soundcloud(bai_hat)
        if not results:
            await interaction.followup.send(f"Kê tìm mỏi mắt trên SoundCloud chẳng thấy bài **'{bai_hat}'** nào cả! Gõ lại xem! 💢")
            return

        await interaction.followup.send("Kết quả tìm kiếm trên SoundCloud đây! Chọn nhanh lên ta còn hát: 😳", view=SongSelectView(results, guild_id))
    except Exception as e: 
        await interaction.followup.send(f"Hức... Họng Kê bị làm sao rồi: `{e}`")

@bot.tree.command(name="replay", description="Bật/Tắt chế độ lặp lại nhạc")
@app_commands.describe(mode="Chọn chế độ lặp lại")
@app_commands.choices(mode=[
    app_commands.Choice(name="Lặp bài hiện tại", value="SONG"),
    app_commands.Choice(name="Lặp cả danh sách", value="QUEUE"),
    app_commands.Choice(name="Tắt lặp lại", value="OFF")
])
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    guild_id = interaction.guild.id; loop_mode[guild_id] = mode.value
    if mode.value == "SONG": msg = "Ờ thì lặp lại bài này! Nghe mãi không chán à đồ ngốc? 💢"
    elif mode.value == "QUEUE": msg = "Lặp lại cả danh sách! Nhớ đấy, bắt Kê hát khô cả họng cho xem! 😤"
    else: msg = "Đã tắt lặp lại! Phù, may quá, không bị bắt hát đi hát lại như con vẹt nữa! 😌"
    await interaction.response.send_message(msg)

@bot.tree.command(name="skip", description="Bỏ qua bài đang phát để chuyển sang bài tiếp theo")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        guild_id = interaction.guild.id
        if loop_mode.get(guild_id) == "SONG":
            loop_mode[guild_id] = "OFF"
            await interaction.response.send_message("Xí, chê hát dở nên bắt đổi bài chứ gì? Tắt lặp bài này luôn rồi! Hứ! 💢")
        else: 
            await interaction.response.send_message("Đổi thì đổi! Khó tính quá! 🙄")
        voice_client.stop() 
    else: 
        await interaction.response.send_message("Có bài nào đang hát đâu mà đòi chuyển hả đồ ngốc?!", ephemeral=True)

@bot.tree.command(name="playlist", description="Xem danh sách bài hát đang chờ trong hàng phát")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    status_text = " [Chế độ: Đang lặp bài hiện tại]" if loop_mode.get(guild_id) == "SONG" else (" [Chế độ: Đang lặp cả danh sách]" if loop_mode.get(guild_id) == "QUEUE" else "")
    if guild_id in music_queues and len(music_queues[guild_id]) > 0:
        q_list = "\n".join([f"{i+1}. {song['title']}" for i, song in enumerate(music_queues[guild_id][:10])])
        if len(music_queues[guild_id]) > 10: q_list += f"\n... chậc, và còn {len(music_queues[guild_id]) - 10} bài nữa Kê lười đọc lắm!"
        await interaction.response.send_message(f"**Danh sách chờ:** {status_text}\n{q_list}")
    else:
        now_playing = current_playing.get(guild_id)
        if now_playing: await interaction.response.send_message(f"Hàng chờ trống rỗng! Đang hát mỗi bài: **{now_playing['title']}** {status_text}")
        else: await interaction.response.send_message("Không có bài nào cả! Nhập lệnh `/play` đi đồ ngốc!")

@bot.tree.command(name="pause", description="Tạm dừng bài hát đang phát")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause(); await interaction.response.send_message("Nghỉ giải lao một tí... 😌")
    else: await interaction.response.send_message("Đang yên ắng mà bắt dừng cái gì?! 💢", ephemeral=True)

@bot.tree.command(name="resume", description="Tiếp tục phát bài hát đã tạm dừng")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume(); await interaction.response.send_message("Lại bắt hát tiếp đây này! 😤")
    else: await interaction.response.send_message("Nhạc có bị dừng đâu mà tiếp tục? Đồ ngốc!", ephemeral=True)

@bot.tree.command(name="stop", description="Dừng phát nhạc và đuổi bot ra khỏi kênh thoại")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        guild_id = interaction.guild.id
        if guild_id in music_queues: music_queues[guild_id].clear() 
        current_playing.pop(guild_id, None); loop_mode.pop(guild_id, None)       
        await voice_client.disconnect(); await interaction.response.send_message("Kê đi ra đây! Không thèm phục vụ ngươi nữa! Hứ! 💢")
    else: await interaction.response.send_message("Kê có ở trong kênh thoại đâu mà đòi đuổi?!", ephemeral=True)

# ==================== SLASH COMMANDS - LIVESTREAM RTMP ====================
@bot.tree.command(name="stream_start", description="Bắt đầu phát livestream video lên YouTube / Facebook / TikTok")
@app_commands.describe(
    nen_tang="Chọn nền tảng livestream",
    stream_key="Khóa luồng (Stream Key) lấy từ trang quản trị Live",
    video_path="Đường dẫn file video trên máy/VPS hoặc URL Web (YouTube, TikTok, Facebook,...)",
    custom_rtmp="URL Server RTMP thủ công (Chỉ cần khi chọn 'Khác / TikTok')",
    loop="Lặp lại video liên tục 24/7 (Mặc định: True)"
)
@app_commands.choices(nen_tang=[
    app_commands.Choice(name="YouTube Live", value="YouTube"),
    app_commands.Choice(name="Facebook Live", value="Facebook"),
    app_commands.Choice(name="Khác / TikTok Live", value="Custom")
])
async def stream_start(
    interaction: discord.Interaction, 
    nen_tang: app_commands.Choice[str], 
    stream_key: str, 
    video_path: str,
    custom_rtmp: str = None,
    loop: bool = True
):
    if interaction.user.id not in OWNER_IDS and not interaction.permissions.administrator:
        await interaction.response.send_message(" Bạn không có quyền khởi chạy Livestream!", ephemeral=True)
        return

    guild_id = interaction.guild_id

    if guild_id in active_streams:
        proc = active_streams[guild_id]["process"]
        if proc.poll() is None:
            await interaction.response.send_message(
                f" Đang có một luồng livestream chạy trên nền tảng **{active_streams[guild_id]['platform']}**! "
                "Dùng `/stream_stop` để tắt trước khi mở luồng mới.", 
                ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=True)

    if nen_tang.value == "Custom":
        if not custom_rtmp:
            await interaction.followup.send(" Khi chọn nền tảng 'Khác / TikTok', bạn bắt buộc phải điền tham số `custom_rtmp`!", ephemeral=True)
            return
        base_rtmp = custom_rtmp.rstrip('/') + '/'
    else:
        base_rtmp = RTMP_SERVERS[nen_tang.value]

    full_target_url = f"{base_rtmp}{stream_key.strip()}"

    real_video_url = video_path.strip()
    is_web_url = real_video_url.startswith("http://") or real_video_url.startswith("https://")

    if is_web_url:
        try:
            ydl_opts = get_ytdl_opts({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True
            })
            def extract_url():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(real_video_url, download=False)
                    return info.get('url') or real_video_url

            extracted = await asyncio.to_thread(extract_url)
            if extracted:
                real_video_url = extracted
        except Exception as e:
            await interaction.followup.send(f" Lỗi trích xuất luồng từ URL qua yt-dlp: `{e}`", ephemeral=True)
            return
    else:
        if not os.path.exists(real_video_url):
            await interaction.followup.send(f" File video cục bộ không tồn tại: `{real_video_url}`", ephemeral=True)
            return

    ffmpeg_cmd = [FFMPEG_PATH]

    if is_web_url:
        ffmpeg_cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ])

    ffmpeg_cmd.append("-re")

    if loop:
        ffmpeg_cmd.extend(["-stream_loop", "-1"])

    ffmpeg_cmd.extend([
        "-i", real_video_url,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", "3000k",
        "-maxrate", "3000k",
        "-bufsize", "6000k",
        "-pix_fmt", "yuv420p",
        "-g", "50",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-f", "flv",
        full_target_url
    ])

    try:
        process = subprocess.Popen(
            ffmpeg_cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.PIPE
        )

        await asyncio.sleep(2)
        if process.poll() is not None:
            _, stderr = process.communicate()
            err_msg = stderr.decode('utf-8', errors='ignore')[-300:]
            await interaction.followup.send(f" Khởi chạy FFmpeg thất bại!\n```text\n{err_msg}\n```", ephemeral=True)
            return

        active_streams[guild_id] = {
            "process": process,
            "platform": nen_tang.name,
            "source": video_path,
            "start_time": time.time()
        }

        embed = discord.Embed(title="🔴 BẮT ĐẦU LIVESTREAM", color=0xe74c3c)
        embed.add_field(name="Nền tảng:", value=nen_tang.name, inline=True)
        embed.add_field(name="Chế độ lặp:", value="Có (24/7)" if loop else "Không", inline=True)
        embed.add_field(name="Nguồn Video:", value=f"`{video_path[:60]}`", inline=False)
        embed.set_footer(text="Gõ /stream_stop để dừng phát sóng bất kỳ lúc nào.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f" Lỗi hệ thống: `{e}`", ephemeral=True)

@bot.tree.command(name="stream_stop", description="Dừng luồng livestream đang phát")
async def stream_stop(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS and not interaction.permissions.administrator:
        await interaction.response.send_message(" Bạn không có quyền dừng Livestream!", ephemeral=True)
        return

    guild_id = interaction.guild_id

    if guild_id not in active_streams:
        await interaction.response.send_message("Hiện không có luồng livestream nào đang chạy!", ephemeral=True)
        return

    stream_data = active_streams[guild_id]
    process = stream_data["process"]

    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

        platform = stream_data["platform"]
        del active_streams[guild_id]

        await interaction.response.send_message(f" Đã tắt luồng livestream trên nền tảng **{platform}** thành công!")
    except Exception as e:
        await interaction.response.send_message(f" Lỗi khi tắt luồng: `{e}`", ephemeral=True)

@bot.tree.command(name="stream_status", description="Kiểm tra trạng thái livestream đang chạy")
async def stream_status(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    if guild_id not in active_streams or active_streams[guild_id]["process"].poll() is not None:
        await interaction.response.send_message("Hiện tại máy chủ này **không phát livestream** nào cả.")
        return

    stream_data = active_streams[guild_id]
    uptime_seconds = int(time.time() - stream_data["start_time"])
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))

    embed = discord.Embed(title="TRẠNG THÁI LIVESTREAM", color=0x2ecc71)
    embed.add_field(name="Nền tảng:", value=stream_data["platform"], inline=True)
    embed.add_field(name="Thời gian đã phát:", value=f"`{uptime_str}`", inline=True)
    embed.add_field(name="Nguồn video:", value=f"`{stream_data['source']}`", inline=False)
    
    await interaction.response.send_message(embed=embed)

# ==================== CHẠY BOT ====================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
