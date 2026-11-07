import os
import sys
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

def get_env(key: str, default: str = "", required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and (value is None or str(value).strip() == ""):
        print(f"❌ [LỖI CONFIG] Thiếu biến môi trường bắt buộc: {key}")
        sys.exit(1)
    return str(value).strip() if value else ""

def get_env_list_int(key: str, default_list: List[int]) -> List[int]:
    val = os.getenv(key, "")
    if not val:
        return default_list
    try:
        return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
    except Exception:
        return default_list

@dataclass(frozen=True)
class AppConfig:
    # --- Discord & Bot Core ---
    bot_token: str = field(default_factory=lambda: get_env("TOKEN", required=True))
    port: int = field(default_factory=lambda: int(get_env("PORT", "25989")))
    host_domain: str = field(default_factory=lambda: get_env("HOST_DOMAIN", ""))
    admin_secret: str = field(default_factory=lambda: get_env("ADMIN_SECRET", required=True))
    
    # --- API Keys ---
    cerebras_api_key: str = field(default_factory=lambda: get_env("CEREBRAS_API_KEY", required=True))
    groq_api_key: str = field(default_factory=lambda: get_env("GROQ_API_KEY", ""))
    ainative_api_key: str = field(default_factory=lambda: get_env("AINATIVE_API_KEY", ""))
    
    # --- ElevenLabs Voice Engine ---
    elevenlabs_api_key: str = field(default_factory=lambda: get_env("ELEVENLABS_API_KEY", ""))
    elevenlabs_voice_id: str = field(default_factory=lambda: get_env("ELEVENLABS_VOICE_ID", ""))
    
    # --- AI Model & Proxy ---
    model_name: str = field(default_factory=lambda: get_env("MODEL_NAME", "gemma-4-31b"))
    proxy_url: str = field(default_factory=lambda: get_env("PROXY_URL", ""))
    
    # --- Firebase Database ---
    firebase_url: str = field(
        default_factory=lambda: get_env("FIREBASE_URL", required=True).rstrip("/")
    )
    
    # --- System & Permissions ---
    reset_cooldown_seconds: int = 86400
    owner_ids: List[int] = field(default_factory=lambda: get_env_list_int("OWNER_IDS", [739495906956673095]))
    
    # --- File Paths ---
    memory_file: str = "memory.json"
    config_file: str = "config.json"
    whitelist_file: str = "whitelist.json"

config = AppConfig()
