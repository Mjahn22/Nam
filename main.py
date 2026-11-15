import os
import re
import io
import json
import time
import base64
import shutil
import asyncio
import datetime
import subprocess
import urllib.parse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

try:
    from duckduckgo_search import DDGS
except ImportError:
    from ddgs import DDGS

load_dotenv()

# ==================== VOICE: OPUS / NACL / DAVE ====================
if not discord.opus.is_loaded():
    for _lib in ('libopus.so.0', 'libopus.so', 'libopus-0.dll', 'libopus.dylib'):
        try:
            discord.opus.load_opus(_lib)
            print(f"[+] Đã nạp Opus: {_lib}")
            break
        except Exception:
            pass
    if not discord.opus.is_loaded():
        print("[!] CẢNH BÁO: chưa nạp được Opus -> cần libopus0 (xem nixpacks.toml)")
try:
    import nacl  # noqa: F401
except ImportError:
    print("[!] THIẾU PyNaCl -> pip install discord.py[voice]")
try:
    import davey  # noqa: F401
except ImportError:
    print("[!] THIẾU davey (mã hóa DAVE của Discord) -> pip install davey")

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        FFMPEG_PATH = "ffmpeg"
print(f"[+] FFmpeg: {FFMPEG_PATH}")

# ==================== CẤU HÌNH ====================
try:
    import config as sys_config
except ImportError:
    sys_config = None


def cfg(name, default=None, env=None):
    """Ưu tiên config.py -> biến môi trường -> mặc định."""
    if sys_config is not None and getattr(sys_config, name, None) is not None:
        return getattr(sys_config, name)
    return os.getenv(env or name, default)


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # ghi nguyên tử, không hỏng file khi crash
    except Exception as e:
        print(f"Lỗi lưu {path}: {e}")


BOT_TOKEN = cfg('BOT_TOKEN', env='TOKEN') or os.getenv('BOT_TOKEN')
OPENROUTER_API_KEY = cfg('OPENROUTER_API_KEY')
MODEL_NAME = cfg('MODEL_NAME', "openai/gpt-4o")
MEMORY_MODEL = cfg('MEMORY_MODEL', MODEL_NAME)

_owner_cfg = cfg('OWNER_IDS')
if isinstance(_owner_cfg, str):
    OWNER_IDS = [int(x) for x in _owner_cfg.split(",") if x.strip().isdigit()]
elif _owner_cfg:
    OWNER_IDS = [int(x) for x in _owner_cfg]
else:
    OWNER_IDS = [739495906956673095, 987654321098765432]

_guild_cfg = cfg('GUILD_IDS')
if isinstance(_guild_cfg, str):
    _guild_cfg = [x for x in _guild_cfg.split(",") if x.strip()]
if not _guild_cfg:
    _guild_cfg = load_json("config.json", {}).get("guild_ids", [])
ALLOWED_GUILDS = set()
for _g in _guild_cfg or []:
    try:
        ALLOWED_GUILDS.add(int(_g))
    except (ValueError, TypeError):
        pass

if not BOT_TOKEN or not OPENROUTER_API_KEY:
    print("LỖI: Thiếu TOKEN hoặc OPENROUTER_API_KEY (đặt trong Variables của Railway hoặc .env)!")
    raise SystemExit(1)

MEMORY_FILE = "memory.json"

ai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    max_retries=3,   # tự retry 429/5xx với backoff
    timeout=60,
    default_headers={"X-Title": "Ke Discord Bot"},
)

http_session = None


async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ==================== YT-DLP ====================
YTDL_BASE = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'quiet': True,
    'no_warnings': True,
    'logtostderr': False,
    'http_headers': {'User-Agent': UA},
    'extractor_args': {'youtube': ['player_client=ios,web']},
    'socket_timeout': 15,
}
YTDL_SEM = asyncio.Semaphore(3)  # giới hạn yt-dlp chạy song song, tránh tràn RAM


def get_ytdl_opts(custom=None):
    opts = dict(YTDL_BASE)
    if custom:
        opts.update(custom)
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
    return opts


async def ytdl_extract(url, **opts):
    def _run():
        with yt_dlp.YoutubeDL(get_ytdl_opts(opts)) as ydl:
            return ydl.extract_info(url, download=False)
    async with YTDL_SEM:
        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            print(f"[!] yt-dlp lỗi ({url[:80]}): {e}")
            return None


FFMPEG_BEFORE = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
FFMPEG_OPTS = '-vn'

# ==================== TRẠNG THÁI ====================
music_queues = {}
loop_mode = {}
current_playing = {}
volumes = {}
idle_tasks = {}
empty_tasks = {}
active_streams = {}

RTMP_SERVERS = {
    "YouTube": "rtmp://a.rtmp.youtube.com/live2/",
    "Facebook": "rtmps://live-api-s.facebook.com:443/rtmp/",
}

memory = load_json(MEMORY_FILE, {})
memory_lock = asyncio.Lock()
user_chat_sessions = {}
user_last_call = {}
USER_COOLDOWN = 3.0  # giây, chống spam đốt credit API

VALID_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
VALID_TEXT_EXTS = {'.txt', '.json', '.py', '.js', '.html', '.css', '.md', '.log',
                   '.csv', '.yaml', '.yml', '.ini', '.conf'}
SEARCH_TRIGGERS = ('hôm nay', 'bây giờ', 'mới nhất', 'hiện tại', 'giá', 'tin tức',
                   'thời tiết', 'tìm link', 'có link', str(datetime.datetime.now().year))
IMAGE_TRIGGERS = ('tìm ảnh', 'ảnh của', 'hình của', 'hình ảnh', 'gửi ảnh')
NO_PING = discord.AllowedMentions.none()


# ==================== AI ====================
async def ask_ai(messages, temperature=0.85, max_tokens=2000, model=None):
    r = await ai_client.chat.completions.create(
        model=model or MODEL_NAME, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    return (r.choices[0].message.content or "").strip()


async def fetch_realtime_news(query):
    try:
        return await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=5)))
    except Exception:
        return []


async def fetch_images(query):
    try:
        res = await asyncio.to_thread(lambda: list(DDGS().images(query, max_results=3)))
        return [r['image'] for r in res if 'image' in r]
    except Exception:
        return []


async def build_web_context(prompt):
    if not any(t in prompt.lower() for t in SEARCH_TRIGGERS):
        return ""
    results = await fetch_realtime_news(prompt)
    if not results:
        return ""
    lines = ["\n[THÔNG TIN TRÊN INTERNET]:"]
    for r in results:
        lines.append(f"- {r.get('title', '')}\n  Nội dung: {(r.get('body') or '')[:300]}\n  Link Nguồn: {r.get('href', '')}")
    return "\n".join(lines) + "\n"


async def build_found_images(prompt):
    if any(t in prompt.lower() for t in IMAGE_TRIGGERS):
        return await fetch_images(prompt)
    return []


async def auto_extract_memory(user_id, user_name, user_msg, bot_reply):
    uid = str(user_id)
    if len(user_msg) < 12:  # tin quá ngắn thì khỏi tốn token trích xuất
        return
    try:
        facts = memory.get(uid, [])
        facts_str = "\n".join(f"- {f}" for f in facts) if facts else "Chưa có ký ức nào."
        result = await ask_ai([
            {"role": "system", "content": (
                f"Bạn là hệ thống phân tích trí nhớ. Trích xuất thông tin cá nhân cốt lõi của người dùng '{user_name}'.\n"
                f"Ký ức hiện tại:\n{facts_str}\n\n"
                "QUY TẮC:\n1. Chỉ trích xuất thông tin mới về NGƯỜI DÙNG.\n"
                "2. Nếu không có thông tin mới, chỉ trả lời đúng một từ: NONE.\n"
                "3. Viết dưới dạng gạch đầu dòng ngắn gọn.")},
            {"role": "user", "content": f"Người dùng nói: {user_msg}\nBot trả lời: {bot_reply[:500]}"},
        ], temperature=0.3, max_tokens=150, model=MEMORY_MODEL)

        if not result or result.strip().upper().startswith("NONE"):
            return
        new_facts = [l.strip().lstrip("-•* ").strip() for l in result.split("\n") if l.strip()]
        async with memory_lock:
            cur = memory.setdefault(uid, [])
            changed = False
            for f in new_facts:
                if len(f) > 2 and f not in cur:
                    cur.append(f)
                    changed = True
            if changed:
                memory[uid] = cur[-20:]
                await asyncio.to_thread(save_json, MEMORY_FILE, memory)
    except Exception as e:
        print(f"[!] Lỗi trí nhớ: {e}")


# ==================== MEDIA ====================
async def get_base64_image(url):
    session = await get_http_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.read()
                mime = r.headers.get('Content-Type', 'image/jpeg')
                return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:
        pass
    return None


async def read_text_attachment(att):
    try:
        text = (await att.read()).decode('utf-8', errors='ignore')
        if len(text) > 10000:
            text = text[:10000] + "\n...[Cắt bớt]..."
        return f"\n[FILE {att.filename}]:\n```\n{text}\n```\n"
    except Exception:
        return ""


async def extract_video_context(url):
    if "youtube.com" in url or "youtu.be" in url:
        session = await get_http_session()
        try:
            enc = urllib.parse.quote(url, safe='')
            async with session.get(f"https://www.youtube.com/oembed?url={enc}&format=json",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    d = await r.json()
                    return f"[BỐI CẢNH VIDEO YOUTUBE] Tiêu đề: {d.get('title')} | Kênh: {d.get('author_name')}"
        except Exception:
            pass
    info = await ytdl_extract(url, skip_download=True, extract_flat=True)
    if info:
        return f"[BỐI CẢNH VIDEO] Tiêu đề: {info.get('title', 'Không rõ')}"
    return "[VIDEO] Không đọc được thông tin."


async def direct_media_url(url, fmt):
    info = await ytdl_extract(url, format=fmt)
    return info.get('url') if info else None


# ==================== PHÁT NHẠC ====================
async def search_soundcloud(keyword, max_results=15):
    info = await ytdl_extract(f"scsearch{max_results}:{keyword}", extract_flat=True)
    results = []
    if info and info.get('entries'):
        for e in info['entries']:
            if not e:
                continue
            url = e.get('webpage_url') or e.get('url')
            if not url:
                continue
            title = (e.get('title') or 'Không rõ tên bài').replace(' | Stream Free Listening on SoundCloud', '')
            results.append({'title': title, 'url': url, 'webpage_url': url})
    return results


async def extract_audio_info(url):
    info = await ytdl_extract(url, extract_flat='in_playlist')
    playlist = []
    if info and info.get('entries'):
        for e in info['entries']:
            if e:
                u = e.get('webpage_url') or e.get('url')
                if u:
                    playlist.append((u, e.get('title', 'Không rõ tên bài')))
    elif info:
        u = info.get('webpage_url') or info.get('url')
        if u:
            playlist.append((u, info.get('title', 'Không rõ tên bài')))
    return playlist


def _cancel_task(store, gid):
    t = store.pop(gid, None)
    if t:
        t.cancel()


def _schedule(store, gid, vc, delay, cond):
    _cancel_task(store, gid)

    async def job():
        try:
            await asyncio.sleep(delay)
            if vc.is_connected() and cond():
                await full_stop(gid, vc)
        except asyncio.CancelledError:
            pass
    store[gid] = asyncio.create_task(job())


async def full_stop(gid, vc):
    music_queues.pop(gid, None)
    current_playing.pop(gid, None)
    loop_mode.pop(gid, None)
    _cancel_task(idle_tasks, gid)
    _cancel_task(empty_tasks, gid)
    try:
        if vc and vc.is_connected():
            await vc.disconnect()
    except Exception as e:
        print(f"[!] Lỗi ngắt kết nối voice: {e}")


def enqueue(gid, vc, songs):
    """Thêm bài vào hàng chờ. Trả về bài bắt đầu phát ngay (hoặc None).
    Không có await bên trong nên không bị race condition."""
    q = music_queues.setdefault(gid, [])
    q.extend(songs)
    if gid not in current_playing and not vc.is_playing() and not vc.is_paused():
        first = q.pop(0)
        current_playing[gid] = first
        asyncio.create_task(play_stream_async(gid, vc, first))
        return first
    return None


async def play_stream_async(gid, vc, song):
    raw_url = song.get('url')
    if not raw_url or not vc or not vc.is_connected():
        play_next(gid, vc)
        return
    _cancel_task(idle_tasks, gid)

    stream_url, headers = None, {}
    info = await ytdl_extract(raw_url, format='bestaudio[protocol^=http][protocol!*=m3u8]/bestaudio/best',
                              ignoreerrors=False)
    if info:
        stream_url = info.get('url')
        headers = info.get('http_headers') or {}
        if not stream_url and info.get('formats'):
            fmts = [f for f in info['formats'] if f.get('acodec') != 'none' and f.get('vcodec') in (None, 'none')]
            if fmts:
                stream_url = fmts[-1].get('url')
                headers = fmts[-1].get('http_headers') or headers

    if not stream_url and any(x in raw_url for x in ('.m3u8', '.flv', '.mp3', '.aac', '.ogg')):
        stream_url = raw_url

    if not stream_url:
        print(f"[!] Không lấy được stream cho: {song.get('title')} -> bỏ qua")
        if loop_mode.get(gid) == "SONG":
            loop_mode[gid] = "OFF"
        play_next(gid, vc)
        return

    try:
        ua = headers.get('User-Agent') or UA
        base = discord.FFmpegPCMAudio(
            stream_url, executable=FFMPEG_PATH,
            before_options=f'{FFMPEG_BEFORE} -user_agent "{ua}"', options=FFMPEG_OPTS)
        source = discord.PCMVolumeTransformer(base, volume=volumes.get(gid, 1.0))

        def after(err):
            if err:
                print(f"[!] Lỗi FFmpeg: {err}")
            bot.loop.call_soon_threadsafe(play_next, gid, vc)

        vc.play(source, after=after)
        print(f"[+] Đang phát: {song.get('title')}")
    except Exception as e:
        print(f"[!] Lỗi khởi tạo FFmpeg: {e}")
        if loop_mode.get(gid) == "SONG":
            loop_mode[gid] = "OFF"
        play_next(gid, vc)


def play_next(gid, vc):
    if not vc or not vc.is_connected():
        current_playing.pop(gid, None)
        return

    mode = loop_mode.get(gid, "OFF")
    last = current_playing.get(gid)
    if mode == "QUEUE" and last:
        music_queues.setdefault(gid, []).append(last)

    if mode == "SONG" and last:
        nxt = last
    elif music_queues.get(gid):
        nxt = music_queues[gid].pop(0)
    else:
        current_playing.pop(gid, None)
        # Hết nhạc: tự rời kênh sau 5 phút nếu vẫn không có gì phát
        _schedule(idle_tasks, gid, vc, 300,
                  lambda: gid not in current_playing and not vc.is_playing() and not vc.is_paused())
        return

    current_playing[gid] = nxt
    asyncio.create_task(play_stream_async(gid, vc, nxt))


# ==================== UI CHỌN BÀI ====================
class SongSelectView(discord.ui.View):
    PER_PAGE = 5

    def __init__(self, results, gid):
        super().__init__(timeout=120)
        self.results = results
        self.gid = gid
        self.page = 0
        self.render()

    def render(self):
        self.clear_items()
        start = self.page * self.PER_PAGE
        end = start + self.PER_PAGE
        chunk = self.results[start:end]

        select = discord.ui.Select(
            placeholder=f"Trang {self.page + 1}: Chọn bài đi đồ ngốc!",
            options=[discord.SelectOption(label=f"{start + i + 1}. {r['title'][:90]}", value=str(start + i))
                     for i, r in enumerate(chunk)])
        select.callback = self.on_select
        self.add_item(select)

        if self.page > 0:
            b = discord.ui.Button(label="⬅ Trang trước", style=discord.ButtonStyle.secondary)
            b.callback = self.on_prev
            self.add_item(b)
        if end < len(self.results):
            b = discord.ui.Button(label="Trang sau ➡", style=discord.ButtonStyle.primary)
            b.callback = self.on_next
            self.add_item(b)
        b = discord.ui.Button(label="Tìm từ khóa khác", style=discord.ButtonStyle.danger)
        b.callback = self.on_search_again
        self.add_item(b)

    async def on_select(self, interaction: discord.Interaction):
        song = self.results[int(interaction.data['values'][0])]
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.edit_message(content="Bot đã bị ngắt kết nối khỏi kênh thoại!", view=None)
            return
        item = {'url': song.get('webpage_url') or song.get('url'), 'title': song['title']}
        started = enqueue(self.gid, vc, [item])
        if started:
            msg = f"Được rồi! Kê sẽ hát: **{started['title']}**. Nghe cho thủng màng nhĩ đi! 😳"
        else:
            msg = f"📌 Đã đẩy **{song['title']}** ra đằng sau hàng chờ! Đợi đi đồ ngốc!"
        await interaction.response.edit_message(content=msg, view=None)

    async def on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.render()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.page += 1
        self.render()
        await interaction.response.edit_message(view=self)

    async def on_search_again(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self.gid))


class SearchModal(discord.ui.Modal, title="Tìm bài hát mới trên SoundCloud"):
    keyword = discord.ui.TextInput(label="Nhập tên bài hát SoundCloud:",
                                   placeholder="Ví dụ: Lofi chill, remix...", required=True)

    def __init__(self, gid):
        super().__init__()
        self.gid = gid

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        results = await search_soundcloud(self.keyword.value)
        if not results:
            await interaction.followup.send("Hứ! Tìm trên SoundCloud không ra bài nào hết! Đổi từ khóa khác đi! 💢", ephemeral=True)
            return
        await interaction.followup.send(f"Đây là kết quả SoundCloud mới cho: **{self.keyword.value}**",
                                        view=SongSelectView(results, self.gid))


def chunk_text(text, limit=1900):
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


# ==================== BOT ====================
class KeBot(commands.Bot):
    async def setup_hook(self):
        await get_http_session()
        try:
            if ALLOWED_GUILDS:
                for gid in ALLOWED_GUILDS:
                    obj = discord.Object(id=gid)
                    self.tree.copy_global_to(guild=obj)
                    await self.tree.sync(guild=obj)
            else:
                await self.tree.sync()
        except Exception as e:
            print(f"[!] Lỗi sync lệnh: {e}")

    async def close(self):
        for data in list(active_streams.values()):
            _kill_stream(data)
        active_streams.clear()
        if http_session and not http_session.closed:
            await http_session.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = KeBot(command_prefix="!", intents=intents)
_ready_once = False


@bot.event
async def on_ready():
    global _ready_once
    print(f"=== Kê đã sẵn sàng cãi lộn ({bot.user}) ===")
    if _ready_once:
        return
    _ready_once = True
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="Đang sục cặc"))
    if ALLOWED_GUILDS:
        for g in list(bot.guilds):
            if g.id not in ALLOWED_GUILDS:
                try:
                    await g.leave()
                except Exception:
                    pass


@bot.event
async def on_guild_join(guild):
    if ALLOWED_GUILDS and guild.id not in ALLOWED_GUILDS:
        try:
            await guild.leave()
        except Exception:
            pass


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    if not vc or not vc.channel:
        return
    gid = guild.id
    if any(not m.bot for m in vc.channel.members):
        _cancel_task(empty_tasks, gid)
    else:  # không còn ai trong kênh -> rời sau 60 giây
        _schedule(empty_tasks, gid, vc, 60,
                  lambda: vc.channel is not None and not any(not m.bot for m in vc.channel.members))


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Không đủ quyền mà đòi sai Kê hả đồ ngốc? 💢"
    else:
        print(f"[!] Lỗi lệnh: {error}")
        msg = f"Lỗi rồi: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ==================== CHAT AI ====================
def build_system_prompt(author):
    uid = str(author.id)
    facts = memory.get(uid, [])
    facts_str = "\n".join(f"- {f}" for f in facts) if facts else "(chưa có)"
    now = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return {"role": "system", "content": (
        f"Bạn là Kê. Ký ức về {author.display_name}:\n{facts_str}\n"
        f"Hôm nay là {now}. "
        "Quy tắc: LUÔN trích dẫn 'Link Nguồn' (URL) nếu có. "
        "NẾU TẠO FILE, bọc toàn bộ nội dung file vào thẻ [FILE: ten_file.ext] và [/FILE].\n"
        "Bạn là tên Kê, một phiên bản thứ 2 của Kê Lão Đại. Thông minh, hài hước, am hiểu mọi thứ. "
        "Cá tính: tự nhiên, dịu dàng nhưng CÓ CHÚT TSUNDERE: ngoài lạnh trong nóng. "
        "Hay nói 'Đồ ngốc', 'Đừng hiểu lầm', gắt gỏng khi bị trêu nhưng vẫn giúp. Dùng nhiều icon (💢, 😳, 😤, 🙄). "
        "Thích làm thơ, lâu lâu chèn một đoạn thơ có vần hay. Dài ngắn tùy ngữ cảnh.")}


async def send_ai_reply(message, reply_text):
    pattern = r'\[FILE:\s*([^\]]+)\](.*?)\[/FILE\]'
    files = [discord.File(io.BytesIO(m.group(2).strip().encode('utf-8')), filename=m.group(1).strip())
             for m in re.finditer(pattern, reply_text, re.DOTALL)]
    clean = re.sub(pattern, '', reply_text, flags=re.DOTALL).strip()

    if clean:
        chunks = chunk_text(clean)
        for i, chunk in enumerate(chunks):
            f = files if i == 0 else []
            if len(chunks) == 1:
                await message.reply(embed=discord.Embed(description=chunk, color=0x3498db),
                                    files=f, mention_author=False, allowed_mentions=NO_PING)
            elif i == 0:
                await message.reply(chunk, files=f, mention_author=False, allowed_mentions=NO_PING)
            else:
                await message.channel.send(chunk, allowed_mentions=NO_PING)
    elif files:
        await message.reply("Cầm lấy file này đi! Kê không phải người hầu của ngươi đâu đấy! 😤",
                            files=files, mention_author=False, allowed_mentions=NO_PING)


async def handle_ai_message(message):
    uid = str(message.author.id)
    now = time.monotonic()
    if now - user_last_call.get(uid, 0) < USER_COOLDOWN:
        await message.reply("Từ từ thôi đồ ngốc! Kê đâu phải máy phun chữ! 💢", mention_author=False)
        return
    user_last_call[uid] = now

    prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
    image_url, file_context = None, ""
    text_atts = []
    for att in message.attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext in VALID_IMAGE_EXTS and not image_url:
            image_url = att.url
        elif ext in VALID_TEXT_EXTS and att.size < 500000:
            text_atts.append(att)

    video_url = None
    m = re.search(r'(https?://[^\s]+)', prompt)
    if m and any(d in m.group(1).lower() for d in
                 ('youtube.com', 'youtu.be', 'tiktok.com', 'facebook.com', 'fb.watch', 'instagram.com')):
        video_url = m.group(1)

    if not prompt and not image_url and not video_url and not text_atts:
        await message.reply("Kêu ta làm gì? Bộ rảnh lắm hả đồ ngốc? 💢", mention_author=False)
        return

    async with message.channel.typing():
        try:
            # Chạy song song mọi tác vụ chuẩn bị ngữ cảnh -> trả lời nhanh hơn
            async def _none():
                return None
            web_ctx, video_ctx, b64, found_imgs, *file_parts = await asyncio.gather(
                build_web_context(prompt),
                extract_video_context(video_url) if video_url else _none(),
                get_base64_image(image_url) if image_url else _none(),
                build_found_images(prompt),
                *[read_text_attachment(a) for a in text_atts],
            )
            file_context = "".join(file_parts)

            parts = [p for p in (web_ctx, video_ctx, file_context, f"[CÂU HỎI]: {prompt}") if p]
            user_text = "\n".join(parts).strip()
            content = user_text
            if b64:
                content = [{"type": "text", "text": user_text},
                           {"type": "image_url", "image_url": {"url": b64}}]

            history = user_chat_sessions.setdefault(uid, [])
            api_ok = True
            try:
                reply_text = await ask_ai(
                    [build_system_prompt(message.author)] + history + [{"role": "user", "content": content}])
                if not reply_text:
                    reply_text, api_ok = "Kê cạn lời rồi, hỏi lại đi đồ ngốc! 😤", False
            except Exception as e:
                api_ok = False
                reply_text = f"Mạng nhà Kê bị chập mạch rồi đồ ngốc: `{e}`"

            await send_ai_reply(message, reply_text)
            for img in found_imgs:
                await message.channel.send(img)

            if api_ok:
                def trim(s):
                    return s[:800] + ("..." if len(s) > 800 else "")
                history.append({"role": "user", "content": trim(prompt)})
                history.append({"role": "assistant", "content": trim(reply_text)})
                user_chat_sessions[uid] = history[-8:]
                asyncio.create_task(auto_extract_memory(message.author.id, message.author.display_name,
                                                        prompt, reply_text))
        except Exception as e:
            print(f"[!] Lỗi on_message: {e}")
            await message.reply(f"Hệ thống Kê bị hỏng rồi: {e} 💢", mention_author=False)


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if bot.user in message.mentions and not message.mention_everyone:
        await handle_ai_message(message)
    await bot.process_commands(message)


# ==================== SLASH: TIỆN ÍCH ====================
@bot.tree.command(name="sync", description="Đồng bộ lệnh Slash Commands ngay lập tức cho server này")
@app_commands.checks.has_permissions(administrator=True)
async def sync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    bot.tree.copy_global_to(guild=interaction.guild)
    await bot.tree.sync(guild=interaction.guild)
    await interaction.followup.send("Đã đồng bộ lệnh thành công! Thử tải lại (Ctrl+R) Discord đi đồ ngốc. 😤", ephemeral=True)


@bot.tree.command(name="tiktok", description="Chuyển đổi link TikTok sang link tải trực tiếp")
@app_commands.describe(link="Đường link bài viết hoặc video TikTok")
async def tiktok_convert(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    session = await get_http_session()
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        final = link
        try:
            async with session.head(link, allow_redirects=True, timeout=timeout) as r:
                final = str(r.url)
        except Exception:
            async with session.get(link, allow_redirects=True, timeout=timeout) as r:
                final = str(r.url)
        m = re.search(r"(@[^\/]+)/(?:video|photo)/(\d+)", final)
        if m:
            await interaction.followup.send(f"Của ngươi đây: https://d.tiktokez.com/{m.group(1)}/video/{m.group(2)} 🙄")
        else:
            await interaction.followup.send("Link cái kiểu gì thế này? Lỗi rồi! 💢")
    except Exception as e:
        await interaction.followup.send(f"Mạng mẽo chán quá: {e}")


@bot.tree.command(name="fb", description="Chuyển đổi link video Facebook")
@app_commands.describe(link="Đường link video Facebook cần trích xuất")
async def fb_convert(interaction: discord.Interaction, link: str):
    await interaction.response.defer()
    url = await direct_media_url(link, 'best')
    if url:
        await interaction.followup.send(f"Lấy đi: {url} 😤")
    else:
        await interaction.followup.send("Không moi được video từ link này đâu đồ ngốc!")


@bot.tree.command(name="nuke", description="Xóa hàng loạt tin nhắn trong kênh hiện tại")
@app_commands.describe(limit="Số lượng tin nhắn cần xóa (Mặc định: 10000)")
@app_commands.checks.has_permissions(manage_channels=True)
async def nuke(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10000] = 10000):
    await interaction.response.defer()
    deleted = await interaction.channel.purge(limit=limit)
    await interaction.followup.send(f"Đã dọn sạch {len(deleted)} tin rác rưởi. Đừng xả rác nữa!")


@bot.tree.command(name="clearbot", description="Xóa các tin nhắn do chính Bot gửi")
@app_commands.describe(amount="Số lượng tin nhắn cần quét (Mặc định: 50)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clearbot(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000] = 50):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.author == bot.user)
    await interaction.followup.send(f"Đã xóa {len(deleted)} tin của Kê rồi. Vừa lòng chưa?! 😤", ephemeral=True)


# ==================== SLASH: NHẠC ====================
@bot.tree.command(name="play", description="Tìm kiếm bài hát trên SoundCloud và chọn bài ưa thích")
@app_commands.describe(bai_hat="Tên bài hát trên SoundCloud hoặc link nhạc trực tiếp")
async def play(interaction: discord.Interaction, bai_hat: str):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Đồ ngốc! Không vào kênh thoại thì Kê hát cho ma nghe à? 💢")
        return

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    try:
        if not vc:
            vc = await channel.connect(timeout=30.0, reconnect=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except Exception as e:
        print(f"[!] Lỗi kết nối kênh thoại: {e}")
        await interaction.followup.send(f"Kê không vào được kênh thoại: `{e}` 💢")
        return

    gid = interaction.guild.id
    _cancel_task(idle_tasks, gid)

    if bai_hat.startswith(("http://", "https://")):
        if ".m3u8" in bai_hat or ".flv" in bai_hat:
            playlist = [(bai_hat, "Livestream (Trực tiếp)")]
        elif "tiktok.com" in bai_hat and "live" in bai_hat.lower():
            su = await direct_media_url(bai_hat, 'bestaudio/best')
            if not su:
                await interaction.followup.send("Kê không lấy được Live này! 💢")
                return
            playlist = [(su, "TikTok Livestream Audio")]
        else:
            playlist = await extract_audio_info(bai_hat)

        if not playlist:
            await interaction.followup.send("🙄 Gửi link vớ vẩn gì thế đồ ngốc?! 💢")
            return
        started = enqueue(gid, vc, [{'url': u, 'title': t} for u, t in playlist])
        if started:
            extra = f" (+{len(playlist) - 1} bài trong hàng chờ)" if len(playlist) > 1 else ""
            await interaction.followup.send(f"Kê bắt đầu phát: **{started['title']}**{extra}. Cấm chê! 😳")
        else:
            await interaction.followup.send(f"Đã nhét {len(playlist)} bài vào hàng chờ rồi!")
        return

    results = await search_soundcloud(bai_hat)
    if not results:
        await interaction.followup.send(f"Kê tìm mỏi mắt trên SoundCloud chẳng thấy bài **'{bai_hat}'** nào cả! Gõ lại xem! 💢")
        return
    await interaction.followup.send("Kết quả tìm kiếm trên SoundCloud đây! Chọn nhanh lên ta còn hát: 😳",
                                    view=SongSelectView(results, gid))


@bot.tree.command(name="replay", description="Bật/Tắt chế độ lặp lại nhạc")
@app_commands.describe(mode="Chọn chế độ lặp lại")
@app_commands.choices(mode=[
    app_commands.Choice(name="Lặp bài hiện tại", value="SONG"),
    app_commands.Choice(name="Lặp cả danh sách", value="QUEUE"),
    app_commands.Choice(name="Tắt lặp lại", value="OFF"),
])
async def replay(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    loop_mode[interaction.guild.id] = mode.value
    msgs = {
        "SONG": "Ờ thì lặp lại bài này! Nghe mãi không chán à đồ ngốc? 💢",
        "QUEUE": "Lặp lại cả danh sách! Nhớ đấy, bắt Kê hát khô cả họng cho xem! 😤",
        "OFF": "Đã tắt lặp lại! Phù, may quá, không bị bắt hát đi hát lại như con vẹt nữa! 😌",
    }
    await interaction.response.send_message(msgs[mode.value])


@bot.tree.command(name="skip", description="Bỏ qua bài đang phát để chuyển sang bài tiếp theo")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        gid = interaction.guild.id
        if loop_mode.get(gid) == "SONG":
            loop_mode[gid] = "OFF"
            await interaction.response.send_message("Xí, chê hát dở nên bắt đổi bài chứ gì? Tắt lặp bài này luôn rồi! Hứ! 💢")
        else:
            await interaction.response.send_message("Đổi thì đổi! Khó tính quá! 🙄")
        vc.stop()
    else:
        await interaction.response.send_message("Có bài nào đang hát đâu mà đòi chuyển hả đồ ngốc?!", ephemeral=True)


@bot.tree.command(name="playlist", description="Xem danh sách bài hát đang chờ trong hàng phát")
async def queue_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    mode = loop_mode.get(gid)
    status = " [Đang lặp bài hiện tại]" if mode == "SONG" else (" [Đang lặp cả danh sách]" if mode == "QUEUE" else "")
    now = current_playing.get(gid)
    q = music_queues.get(gid, [])
    if not now and not q:
        await interaction.response.send_message("Không có bài nào cả! Nhập lệnh `/play` đi đồ ngốc!")
        return
    text = f"**Đang hát:** {now['title']}{status}\n" if now else ""
    if q:
        text += "**Danh sách chờ:**\n" + "\n".join(f"{i + 1}. {s['title']}" for i, s in enumerate(q[:10]))
        if len(q) > 10:
            text += f"\n... chậc, và còn {len(q) - 10} bài nữa Kê lười đọc lắm!"
    else:
        text += "Hàng chờ trống rỗng!"
    await interaction.response.send_message(text[:1990])


@bot.tree.command(name="nowplaying", description="Xem bài đang phát")
async def nowplaying(interaction: discord.Interaction):
    now = current_playing.get(interaction.guild.id)
    if now:
        await interaction.response.send_message(f"Đang hát: **{now['title']}** 🎶")
    else:
        await interaction.response.send_message("Có hát bài nào đâu mà hỏi đồ ngốc?!", ephemeral=True)


@bot.tree.command(name="volume", description="Chỉnh âm lượng (0-200%)")
@app_commands.describe(percent="Âm lượng từ 0 đến 200")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
    gid = interaction.guild.id
    volumes[gid] = percent / 100
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = percent / 100
    await interaction.response.send_message(f"Đã chỉnh âm lượng thành **{percent}%**! 😤")


@bot.tree.command(name="pause", description="Tạm dừng bài hát đang phát")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Nghỉ giải lao một tí... 😌")
    else:
        await interaction.response.send_message("Đang yên ắng mà bắt dừng cái gì?! 💢", ephemeral=True)


@bot.tree.command(name="resume", description="Tiếp tục phát bài hát đã tạm dừng")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Lại bắt hát tiếp đây này! 😤")
    else:
        await interaction.response.send_message("Nhạc có bị dừng đâu mà tiếp tục? Đồ ngốc!", ephemeral=True)


@bot.tree.command(name="stop", description="Dừng phát nhạc và đuổi bot ra khỏi kênh thoại")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await full_stop(interaction.guild.id, vc)
        await interaction.response.send_message("Kê đi ra đây! Không thèm phục vụ ngươi nữa! Hứ! 💢")
    else:
        await interaction.response.send_message("Kê có ở trong kênh thoại đâu mà đòi đuổi?!", ephemeral=True)


# ==================== SLASH: LIVESTREAM RTMP ====================
def _is_stream_admin(interaction):
    return interaction.user.id in OWNER_IDS or interaction.permissions.administrator


def _kill_stream(data):
    proc = data["process"]
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass
    try:
        data["log"].close()
    except Exception:
        pass


def _read_log_tail(path, n=300):
    try:
        with open(path, 'rb') as f:
            return f.read()[-n:].decode('utf-8', errors='ignore')
    except Exception:
        return ""


@bot.tree.command(name="stream_start", description="Bắt đầu phát livestream video lên YouTube / Facebook / TikTok")
@app_commands.describe(
    nen_tang="Chọn nền tảng livestream",
    stream_key="Khóa luồng (Stream Key) lấy từ trang quản trị Live",
    video_path="Đường dẫn file video trên máy/VPS hoặc URL Web (YouTube, TikTok, Facebook,...)",
    custom_rtmp="URL Server RTMP thủ công (Chỉ cần khi chọn 'Khác / TikTok')",
    loop="Lặp lại video liên tục 24/7 (Mặc định: True)",
)
@app_commands.choices(nen_tang=[
    app_commands.Choice(name="YouTube Live", value="YouTube"),
    app_commands.Choice(name="Facebook Live", value="Facebook"),
    app_commands.Choice(name="Khác / TikTok Live", value="Custom"),
])
async def stream_start(interaction: discord.Interaction, nen_tang: app_commands.Choice[str],
                       stream_key: str, video_path: str, custom_rtmp: str = None, loop: bool = True):
    if not _is_stream_admin(interaction):
        await interaction.response.send_message("Bạn không có quyền khởi chạy Livestream!", ephemeral=True)
        return

    gid = interaction.guild_id
    cur = active_streams.get(gid)
    if cur and cur["process"].poll() is None:
        await interaction.response.send_message(
            f"Đang có một luồng livestream chạy trên **{cur['platform']}**! Dùng `/stream_stop` để tắt trước.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if nen_tang.value == "Custom":
        if not custom_rtmp:
            await interaction.followup.send("Khi chọn 'Khác / TikTok' bạn phải điền `custom_rtmp`!", ephemeral=True)
            return
        base = custom_rtmp.rstrip('/') + '/'
    else:
        base = RTMP_SERVERS[nen_tang.value]
    target = f"{base}{stream_key.strip()}"

    src = video_path.strip()
    is_web = src.startswith(("http://", "https://"))
    if is_web:
        url = await direct_media_url(src, 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best')
        src = url or src
    elif not os.path.exists(src):
        await interaction.followup.send(f"File video cục bộ không tồn tại: `{src}`", ephemeral=True)
        return

    cmd = [FFMPEG_PATH, "-nostdin", "-loglevel", "error"]
    if is_web:
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-user_agent", UA]
    cmd.append("-re")
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", src,
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
            "-pix_fmt", "yuv420p", "-g", "50",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-f", "flv", target]

    # FIX: ghi stderr ra file thay vì PIPE (PIPE không ai đọc sẽ đầy buffer và treo FFmpeg)
    log_path = f"stream_{gid}.log"
    try:
        log = open(log_path, "wb")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        await asyncio.sleep(2)
        if proc.poll() is not None:
            log.close()
            await interaction.followup.send(
                f"Khởi chạy FFmpeg thất bại!\n```text\n{_read_log_tail(log_path)}\n```", ephemeral=True)
            return

        active_streams[gid] = {"process": proc, "log": log, "platform": nen_tang.name,
                               "source": video_path, "start_time": time.time()}
        embed = discord.Embed(title="🔴 BẮT ĐẦU LIVESTREAM", color=0xe74c3c)
        embed.add_field(name="Nền tảng:", value=nen_tang.name, inline=True)
        embed.add_field(name="Chế độ lặp:", value="Có (24/7)" if loop else "Không", inline=True)
        embed.add_field(name="Nguồn Video:", value=f"`{video_path[:60]}`", inline=False)
        embed.set_footer(text="Gõ /stream_stop để dừng phát sóng bất kỳ lúc nào.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Lỗi hệ thống: `{e}`", ephemeral=True)


@bot.tree.command(name="stream_stop", description="Dừng luồng livestream đang phát")
async def stream_stop(interaction: discord.Interaction):
    if not _is_stream_admin(interaction):
        await interaction.response.send_message("Bạn không có quyền dừng Livestream!", ephemeral=True)
        return
    data = active_streams.pop(interaction.guild_id, None)
    if not data:
        await interaction.response.send_message("Hiện không có luồng livestream nào đang chạy!", ephemeral=True)
        return
    await asyncio.to_thread(_kill_stream, data)
    await interaction.response.send_message(f"Đã tắt luồng livestream trên **{data['platform']}** thành công!")


@bot.tree.command(name="stream_status", description="Kiểm tra trạng thái livestream đang chạy")
async def stream_status(interaction: discord.Interaction):
    data = active_streams.get(interaction.guild_id)
    if not data or data["process"].poll() is not None:
        await interaction.response.send_message("Hiện tại máy chủ này **không phát livestream** nào cả.")
        return
    uptime = str(datetime.timedelta(seconds=int(time.time() - data["start_time"])))
    embed = discord.Embed(title="TRẠNG THÁI LIVESTREAM", color=0x2ecc71)
    embed.add_field(name="Nền tảng:", value=data["platform"], inline=True)
    embed.add_field(name="Thời gian đã phát:", value=f"`{uptime}`", inline=True)
    embed.add_field(name="Nguồn video:", value=f"`{data['source'][:100]}`", inline=False)
    await interaction.response.send_message(embed=embed)


# ==================== CHẠY BOT ====================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
