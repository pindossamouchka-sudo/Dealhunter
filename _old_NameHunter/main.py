"""
NameHunter PRO & Cosmic Core — Full Production Backend
Telegram Bot + High-Performance WebApp with 3D Graphics
"""

import os
import sys
import json
import time
import hmac
import hashlib
import secrets
import random
import asyncio
import logging
import signal
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote
from datetime import datetime, timezone

import aiosqlite
import aiohttp
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect, HTTPException,
    Query, Request, Response
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("namehunter")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/namehunter.db")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
INIT_DATA_TTL = int(os.getenv("INIT_DATA_TTL", "86400"))
TON_API_KEY = os.getenv("TON_API_KEY", "")
FRAGMENT_API_URL = os.getenv("FRAGMENT_API_URL", "https://fragment.com/api")
ENABLE_QUANTUM_INTERCEPT = os.getenv("ENABLE_QUANTUM_INTERCEPT", "false").lower() == "true"
ENABLE_GALACTIC_RAID = os.getenv("ENABLE_GALACTIC_RAID", "false").lower() == "true"
ENABLE_TIME_CAPSULE = os.getenv("ENABLE_TIME_CAPSULE", "false").lower() == "true"

MAX_ENERGY = 1000
ENERGY_REGEN_RATE = 50
OFFLINE_MINING_MAX_SECONDS = 3 * 3600
FREE_DAILY_GENERATIONS = 100
SPIN_COOLDOWN = 24 * 3600
RADAR_CHECK_INTERVAL = 60
FRAGMENT_SCRAPE_INTERVAL = 300
PRO_CHECK_INTERVAL = 180

telethon_client = None
db_lock = asyncio.Lock()

PLANET_DATA = [
    {"name": "Земля", "hp": 1000, "reward": 50, "color": "#3b82f6"},
    {"name": "Луна", "hp": 1500, "reward": 75, "color": "#9ca3af"},
    {"name": "Марс", "hp": 2500, "reward": 120, "color": "#ef4444"},
    {"name": "Венера", "hp": 4000, "reward": 200, "color": "#eab308"},
    {"name": "Юпитер", "hp": 7000, "reward": 350, "color": "#f97316"},
    {"name": "Сатурн", "hp": 12000, "reward": 600, "color": "#d4a955"},
    {"name": "Уран", "hp": 20000, "reward": 1000, "color": "#06b6d4"},
    {"name": "Нептун", "hp": 35000, "reward": 1800, "color": "#2563eb"},
    {"name": "Солнце", "hp": 60000, "reward": 3500, "color": "#fbbf24"},
    {"name": "Чёрная Дыра", "hp": 100000, "reward": 8000, "color": "#7c3aed"},
]

NICKNAME_CATEGORIES = {
    "superheroes": "Супергерои",
    "cinema": "Кино",
    "anime": "Аниме",
    "games": "Игры",
    "esports": "Киберспорт",
    "crypto": "Крипта",
    "music": "Музыка",
    "business": "Бизнес",
    "auto": "Авто",
    "sport": "Спорт",
    "it": "IT",
    "luxury": "Роскошь",
    "blog": "Блог",
    "memes": "Мемы",
    "twitch": "Твич",
    "art": "Арт",
    "food": "Еда",
    "cities": "Города",
    "cosmos": "Космос",
    "mythology": "Мифология",
}

CATEGORY_WORDS = {
    "superheroes": [
        "batman", "superman", "spiderman", "ironman", "thor", "hulk",
        "deadpool", "wolverine", "flash", "captain", "blackpanther",
        "doctorstrange", "loki", "thanos", "venom", "antman",
        "hawkeye", "falcon", "scarletwitch", "vision", "warlock",
        "apocalypse", "carnage", "sentry", "hyperion", "omniman",
        "invincible", "spawn", "hellboy", "ghostrider", "punisher",
    ],
    "cinema": [
        "godfather", "matrix", "inception", "gladiator", "interstellar",
        "avatar", "joker", "batman", "terminator", "predator",
        "alien", "robocop", "diehard", "jaws", "psycho",
        "casablanca", "taxidriver", "rocky", "rambo", "topgun",
        "goodfellas", "scarface", "heat", "collateral", "drive",
        "bladerunner", "dune", "oppenheimer", "barbie", "parasite",
    ],
    "anime": [
        "naruto", "goku", "luffy", "zoro", "sanji",
        "tanjiro", "gojo", "sukuna", "eren", "levi",
        "alucard", "saitama", "tatsumaki", "killua", "gon",
        "hisoka", "pikachu", "l", "light", "edward",
        "guts", "griffith", "akira", "totoro",
        "kakashi", "itachi", "madara", "hashirama", "toji",
    ],
    "games": [
        "mario", "luigi", "link", "zelda", "sonic",
        "kratos", "joel", "ellie", "lara",
        "masterchief", "geralt", "ciri", "dante", "nero",
        "cloud", "sephiroth", "tifa", "aerith", "solid_snake",
        "bigboss", "doomguy", "gordon", "freeman", "cortana",
        "pyramidhead", "nathan_drake", "sly", "ratchet", "clank",
    ],
    "esports": [
        "faker", "s1mple", "zywoo", "niko", "device",
        "olofmeister", "get_right", "coldzera",
        "fallen", "dupreeh", "gla1ve", "magisk", "scream",
        "shroud", "stewie2k", "tarik", "nitr0", "hiko",
        "tenz", "asuna", "xqc",
        "sumail", "notail", "miracle", "puppey", "ana",
    ],
    "crypto": [
        "satoshi", "vitalik", "charles", "cz",
        "elonmusk", "michael_saylor",
        "hodl", "defi", "ape", "moon", "laser",
        "whale", "bull", "diamond", "rocket", "lambo",
        "web3", "nft", "dao", "metaverse", "layer2",
    ],
    "music": [
        "drake", "kendrick", "eminem", "tupac", "biggie",
        "jayz", "kanye", "travisscott", "playboicarti", "liluzivert",
        "postmalone", "weeknd", "badbunny", "dualipa", "billie",
        "taylorswift", "beyonce", "rihanna", "dojacat", "sza",
        "frankocean", "tyler", "macmiller", "juicewrlad", "peep",
        "xxtentacion", "lilpeep", "nickiminaj",
    ],
    "business": [
        "elonmusk", "bezos", "zuckerberg", "gates", "jobs",
        "cook", "branson", "buffett", "munger",
        "dalio", "thiel",
        "zhao", "spiegel", "dorsey",
        "huang", "jensen",
    ],
    "auto": [
        "ferrari", "lamborghini", "porsche", "bugatti", "koenigsegg",
        "mclaren", "astonmartin", "maserati", "bentley", "rollsroyce",
        "mustang", "camaro", "corvette", "tesla", "cybertruck",
        "nismo", "amg", "brabus", "abt", "novitec",
        "hennessey", "rimac", "pininfarina", "devel", "zenvo",
        "ford", "gtr", "supra", "evo",
    ],
    "sport": [
        "ronaldo", "messi", "neymar", "mbappe", "haaland",
        "lebron", "jordan", "kobe", "curry", "durant",
        "federer", "djokovic", "nadal", "hamilton", "verstappen",
        "mike_tyson", "mayweather", "mcgregor", "khabib", "adesanya",
        "tiger_woods", "serena", "shaq", "magic", "bird",
        "gretzky", "brady", "pele", "maradona", "beckham",
    ],
    "it": [
        "torvalds", "jobs", "wozniak", "gates", "zuckerberg",
        "page", "brin", "dorsey", "karp",
        "musk", "altman",
        "hacker", "coder", "dev", "cyber", "zero",
        "byte", "node", "rust", "golang", "python",
    ],
    "luxury": [
        "rolex", "cartier", "gucci", "prada", "louisvuitton",
        "chanel", "hermes", "burberry", "versace", "dolce",
        "armani", "fendi", "balenciaga", "moncler",
        "tiffany", "van_cleef", "bvlgari", "patek",
        "richard_mille", "brioni",
        "belmond", "aman", "four_seasons", "ritz",
    ],
    "blog": [
        "mrbeast", "pewdiepie", "markiplier", "jacksepticeye", "linus",
        "mkbhd", "unboxtherapy", "dave2d",
        "smosh", "veritasium", "3blue1brown", "khan",
        "lemmino", "kurzgesagt", "exurb",
        "podcast", "influencer", "vlogger", "streamer", "creator",
        "youtuber", "tiktoker",
    ],
    "memes": [
        "doge", "pepe", "wojak", "chad", "gigachad",
        "stonks", "meme", "troll", "based", "cringe",
        "ratio", "sus", "bruh",
        "noob", "rekt", "poggers", "copium", "hopium",
        "king", "sigma", "alpha",
    ],
    "twitch": [
        "xqc", "shroud", "pokimane", "ninja", "tfue",
        "summit1g", "timthetatman", "drdisrespect", "mizkif", "hasan",
        "loserfruit", "amouranth",
        "sodapoppin", "ludwig",
        "asmongold", "esfand",
    ],
    "art": [
        "picasso", "monet", "davinci", "van_gogh", "michelangelo",
        "rembrandt", "warhol", "pollock", "banksy", "hockney",
        "basquiat", "haring", "kusama", "kahlo",
        "artdeco", "abstract", "surreal", "minimalist", "cubist",
        "digital", "pixel", "vector",
        "canvas", "brush", "palette", "neon", "glow",
    ],
    "food": [
        "gordon", "ramsay", "bourdain", "oliver",
        "flay", "chang",
        "sushi", "ramen", "taco", "pizza", "burger",
        "steak", "pasta", "curry", "pho", "bibimbap",
        "dessert", "chocolate", "espresso", "matcha", "croissant",
        "wagyu", "truffle", "caviar", "champagne",
    ],
    "cities": [
        "tokyo", "london", "paris", "newyork", "dubai",
        "singapore", "hongkong", "sydney", "rome", "barcelona",
        "berlin", "moscow", "istanbul", "seoul", "bangkok",
        "mumbai", "toronto", "shanghai", "amsterdam", "vienna",
        "vegas", "miami", "boston", "seattle",
    ],
    "cosmos": [
        "galaxy", "nebula", "quasar", "pulsar", "supernova",
        "blackhole", "wormhole", "singularity", "antimatter", "darkenergy",
        "darkmatter", "astronaut", "cosmonaut", "spacex", "nasa",
        "artemis", "apollo", "sputnik", "voyager", "hubble",
        "james_webb", "andromeda", "milkyway", "orion", "pleiades",
        "saturn", "jupiter", "mars", "venus", "mercury",
    ],
    "mythology": [
        "zeus", "poseidon", "hades", "athena", "ares",
        "apollo", "artemis", "hermes", "aphrodite", "hephaestus",
        "odin", "thor", "loki", "freya", "tyr",
        "ra", "isis", "osiris", "anubis", "horus",
        "krishna", "shiva", "vishnu", "ganesha", "kali",
        "angel", "demon", "dragon", "phoenix", "griffin",
    ],
}

SPIN_PRIZES = [
    {"type": "coins", "value": 10000, "label": "10,000 \U0001FA99", "weight": 30, "color": "#eab308"},
    {"type": "coins", "value": 35000, "label": "35,000 \U0001FA99", "weight": 20, "color": "#f59e0b"},
    {"type": "coins", "value": 100000, "label": "100,000 \U0001FA99", "weight": 10, "color": "#d97706"},
    {"type": "boost", "value": 120, "label": "X2 Turbo 15m", "weight": 20, "color": "#29c75f"},
    {"type": "radar", "value": 1, "label": "Radar Slot", "weight": 10, "color": "#38bdf8"},
    {"type": "pro_3d", "value": 3, "label": "PRO 3 days", "weight": 5, "color": "#a855f7"},
    {"type": "pro_7d", "value": 7, "label": "PRO 7 days", "weight": 3, "color": "#7c3aed"},
    {"type": "pro_lifetime", "value": 9999, "label": "PRO Lifetime", "weight": 2, "color": "#ef4444"},
]

ACHIEVEMENTS_DEFS = [
    {"key": "first_click", "name": "First Tap", "desc": "Make your first click", "target": 1, "field": "total_clicks"},
    {"key": "clicks_100", "name": "Thousand", "desc": "100 clicks", "target": 100, "field": "total_clicks"},
    {"key": "clicks_1000", "name": "MegaTap", "desc": "1,000 clicks", "target": 1000, "field": "total_clicks"},
    {"key": "clicks_10000", "name": "GigaTap", "desc": "10,000 clicks", "target": 10000, "field": "total_clicks"},
    {"key": "clicks_100000", "name": "TeraTap", "desc": "100,000 clicks", "target": 100000, "field": "total_clicks"},
    {"key": "coins_10k", "name": "Piggy Bank", "desc": "Earn 10,000 coins", "target": 10000, "field": "total_coins_earned"},
    {"key": "coins_100k", "name": "Chest", "desc": "100,000 coins", "target": 100000, "field": "total_coins_earned"},
    {"key": "coins_1m", "name": "Treasury", "desc": "1,000,000 coins", "target": 1000000, "field": "total_coins_earned"},
    {"key": "planet_1", "name": "Earth Crusher", "desc": "Destroy Earth", "target": 1, "field": "planets_destroyed"},
    {"key": "planet_5", "name": "System Destroyer", "desc": "5 planets", "target": 5, "field": "planets_destroyed"},
    {"key": "planet_10", "name": "Galactic God", "desc": "All 10 bodies", "target": 10, "field": "planets_destroyed"},
    {"key": "ref_1", "name": "Scout", "desc": "1 referral", "target": 1, "field": "referrals_count"},
    {"key": "ref_5", "name": "Squad", "desc": "5 referrals", "target": 5, "field": "referrals_count"},
    {"key": "ref_10", "name": "Commander", "desc": "10 referrals", "target": 10, "field": "referrals_count"},
    {"key": "spin_10", "name": "Fortune", "desc": "10 spins", "target": 10, "field": "total_spins"},
    {"key": "pro_user", "name": "PRO", "desc": "Activate PRO", "target": 1, "field": "pro_activated"},
    {"key": "nick_master", "name": "Nick Master", "desc": "50 nick checks", "target": 50, "field": "total_generations"},
    {"key": "radar_hunter", "name": "Radar Hunter", "desc": "10 radar targets", "target": 10, "field": "radar_targets_count"},
]

FRAGMENT_GIFTS = [
    {"name": "Crystal Pepe", "slug": "crystal_pepe", "floor": 1.5, "image": "pepe"},
    {"name": "Gold Flower", "slug": "gold_flower", "floor": 2.0, "image": "flower"},
    {"name": "Snoop Car", "slug": "snoop_car", "floor": 5.0, "image": "car"},
    {"name": "TG Cap", "slug": "tg_cap", "floor": 0.8, "image": "cap"},
    {"name": "Diamond Ring", "slug": "diamond_ring", "floor": 10.0, "image": "ring"},
    {"name": "Neon Crown", "slug": "neon_crown", "floor": 3.0, "image": "crown"},
    {"name": "Cyber Skull", "slug": "cyber_skull", "floor": 7.5, "image": "skull"},
    {"name": "Cosmic Heart", "slug": "cosmic_heart", "floor": 1.2, "image": "heart"},
    {"name": "Pixel Rocket", "slug": "pixel_rocket", "floor": 4.0, "image": "rocket"},
    {"name": "Void Eye", "slug": "void_eye", "floor": 15.0, "image": "eye"},
]

# =============================================================================
# Database Layer
# =============================================================================

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-32768")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT DEFAULT 'ru',
                is_pro INTEGER DEFAULT 0,
                pro_expires_at INTEGER DEFAULT 0,
                coins INTEGER DEFAULT 0,
                energy INTEGER DEFAULT 1000,
                max_energy INTEGER DEFAULT 1000,
                energy_regen_rate INTEGER DEFAULT 50,
                multi_tap_level INTEGER DEFAULT 1,
                offline_income_level INTEGER DEFAULT 1,
                last_click_at INTEGER DEFAULT 0,
                last_online_at INTEGER DEFAULT 0,
                total_clicks INTEGER DEFAULT 0,
                total_coins_earned INTEGER DEFAULT 0,
                current_planet_index INTEGER DEFAULT 0,
                planet_hp INTEGER DEFAULT 1000,
                planet_max_hp INTEGER DEFAULT 1000,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_spins_earned INTEGER DEFAULT 0,
                free_spins_available INTEGER DEFAULT 1,
                last_free_spin_at INTEGER DEFAULT 0,
                daily_generations_used INTEGER DEFAULT 0,
                last_generation_reset_at INTEGER DEFAULT 0,
                promo_attempts INTEGER DEFAULT 0,
                promo_locked_until INTEGER DEFAULT 0,
                total_spins INTEGER DEFAULT 0,
                total_generations INTEGER DEFAULT 0,
                planets_destroyed INTEGER DEFAULT 0,
                pro_activated INTEGER DEFAULT 0,
                radar_targets_count INTEGER DEFAULT 0,
                radar_slots INTEGER DEFAULT 1,
                boost_multiplier REAL DEFAULT 1.0,
                boost_expires_at INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_referral ON users(referral_code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_referred ON users(referred_by)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                reward_type TEXT NOT NULL,
                reward_value INTEGER NOT NULL,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                expires_at INTEGER,
                is_active INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS radar_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_username TEXT NOT NULL,
                status TEXT DEFAULT 'watching',
                notified_at INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_radar_user ON radar_targets(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_radar_status ON radar_targets(status)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id INTEGER PRIMARY KEY,
                total_clicks INTEGER DEFAULT 0,
                total_coins INTEGER DEFAULT 0,
                planets_destroyed INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS generation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                category TEXT NOT NULL,
                rarity TEXT NOT NULL,
                is_available INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON generation_history(user_id)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_key TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                completed_at INTEGER,
                claimed_at INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE(user_id, achievement_key)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS fragment_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gift_name TEXT NOT NULL,
                gift_slug TEXT NOT NULL,
                price_ton REAL NOT NULL,
                original_price_ton REAL,
                discount_percent INTEGER DEFAULT 0,
                floor_price_ton REAL,
                supply INTEGER,
                sold INTEGER,
                image_url TEXT,
                is_active INTEGER DEFAULT 1,
                scraped_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fragment_active ON fragment_offers(is_active)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS quantum_intercepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_username TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                tx_hash TEXT,
                nft_address TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                completed_at INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS galactic_raids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                boss_planet_index INTEGER NOT NULL,
                boss_hp INTEGER NOT NULL,
                boss_max_hp INTEGER NOT NULL,
                started_at INTEGER DEFAULT (strftime('%s', 'now')),
                ends_at INTEGER,
                status TEXT DEFAULT 'active',
                total_damage INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS raid_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raid_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                damage_dealt INTEGER DEFAULT 0,
                reward_claimed INTEGER DEFAULT 0,
                FOREIGN KEY (raid_id) REFERENCES galactic_raids(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE(raid_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS time_capsules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                category TEXT NOT NULL,
                rarity TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                is_claimed INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        await db.commit()
        logger.info("Database migrations completed")
    finally:
        await db.close()


# =============================================================================
# Telegram initData Validation
# =============================================================================

def validate_init_data(init_data: str) -> Optional[dict]:
    try:
        parsed = parse_qs(init_data)
        auth_data = {}
        for key, values in parsed.items():
            auth_data[key] = values[0]

        if "hash" not in auth_data:
            return None

        received_hash = auth_data.pop("hash")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(auth_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, computed_hash):
            return None

        auth_date = int(auth_data.get("auth_date", "0"))
        if time.time() - auth_date > INIT_DATA_TTL:
            return None

        return auth_data
    except Exception as e:
        logger.error(f"InitData validation error: {e}")
        return None


# =============================================================================
# Telethon Username Checker
# =============================================================================

async def init_telethon():
    global telethon_client
    if API_ID and API_HASH and STRING_SESSION:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            telethon_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
            await telethon_client.start()
            logger.info("Telethon client initialized")
        except Exception as e:
            logger.warning(f"Telethon init failed: {e}. Using HTTP fallback only.")
            telethon_client = None
    else:
        logger.info("Telethon credentials not provided. Using HTTP fallback only.")


async def check_username_available(username: str) -> dict:
    username = username.strip().lower().lstrip("@")

    if telethon_client and telethon_client.is_connected():
        try:
            from telethon.tl.functions.account import CheckUsernameRequest
            result = await telethon_client(CheckUsernameRequest(username))
            return {"available": result, "method": "mtproto"}
        except Exception as e:
            logger.warning(f"MTProto check failed for {username}: {e}, falling back to HTTP")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://t.me/{username}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = await resp.text()
                if resp.status == 404:
                    return {"available": True, "method": "http"}
                if "This username is already taken" in text:
                    return {"available": False, "method": "http"}
                if "tgme_page_extra" in text:
                    return {"available": False, "method": "http"}
                return {"available": None, "method": "http", "note": "ambiguous"}
    except Exception as e:
        logger.error(f"HTTP check failed for {username}: {e}")
        return {"available": None, "method": "error", "error": str(e)}


# =============================================================================
# Rarity System
# =============================================================================

def get_rarity() -> str:
    roll = random.random()
    if roll < 0.005:
        return "SSS+"
    elif roll < 0.03:
        return "SS"
    elif roll < 0.15:
        return "S"
    else:
        return "A"


def get_rarity_color(rarity: str) -> str:
    colors = {
        "SSS+": "#ef4444",
        "SS": "#a855f7",
        "S": "#38bdf8",
        "A": "#29c75f",
    }
    return colors.get(rarity, "#29c75f")


def generate_nickname(category: str) -> str:
    words = CATEGORY_WORDS.get(category, CATEGORY_WORDS["cosmos"])
    base = random.choice(words).strip().replace(" ", "")
    suffix = random.choice([
        "", str(random.randint(0, 99)), "_", "__", "x", "X",
        "xd", "gg", "yt", "tv", "tt", "ig", "tg",
    ])
    prefix = random.choice([
        "", "the", "real", "pro", "x", "xx", "mr", "lord",
        "king", "god", "astro", "cyber", "neo", "ultra", "mega",
        "hyper", "super",
    ])
    sep = random.choice(["", "_", ".", ""])

    patterns = [
        f"{prefix}{sep}{base}{suffix}",
        f"{base}{sep}{suffix}",
        f"{prefix}{sep}{base}",
        f"{base}{random.randint(10, 99)}",
    ]
    result = random.choice(patterns).strip("_.").lower()
    if len(result) < 3:
        result = base + str(random.randint(100, 999))
    if len(result) > 32:
        result = result[:32]
    return result


# =============================================================================
# Background Workers
# =============================================================================

async def radar_monitor_worker():
    logger.info("Radar monitor worker started")
    while True:
        try:
            db = await get_db()
            try:
                cursor = await db.execute("""
                    SELECT rt.id, rt.user_id, rt.target_username, u.is_pro
                    FROM radar_targets rt
                    JOIN users u ON u.telegram_id = rt.user_id
                    WHERE rt.status = 'watching'
                """)
                targets = await cursor.fetchall()
                for target in targets:
                    result = await check_username_available(target["target_username"])
                    if result.get("available") is True:
                        await db.execute(
                            "UPDATE radar_targets SET status = 'found', notified_at = ? WHERE id = ?",
                            (int(time.time()), target["id"])
                        )
                        await db.commit()
                        try:
                            if aiogram_bot:
                                await aiogram_bot.send_message(
                                    target["user_id"],
                                    f"\U0001F3AF **Radar triggered!**\n\n"
                                    f"Username `{target['target_username']}` is **FREE!**\n"
                                    f"Grab it before someone else does!",
                                    parse_mode="Markdown",
                                )
                        except Exception as e:
                            logger.error(f"Radar notification failed: {e}")
            finally:
                await db.close()
            await asyncio.sleep(RADAR_CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Radar worker error: {e}")
            await asyncio.sleep(RADAR_CHECK_INTERVAL)


async def fragment_scrape_worker():
    logger.info("Fragment scrape worker started")
    while True:
        try:
            db = await get_db()
            try:
                for gift in FRAGMENT_GIFTS:
                    try:
                        discount = random.randint(25, 50)
                        price = round(gift["floor"] * (1 - discount / 100), 2)
                        original = gift["floor"]
                        supply = random.randint(50, 500)
                        sold = random.randint(10, supply - 1)

                        await db.execute("""
                            INSERT OR REPLACE INTO fragment_offers
                            (gift_name, gift_slug, price_ton, original_price_ton, discount_percent,
                             floor_price_ton, supply, sold, image_url, is_active, scraped_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """, (
                            gift["name"], gift["slug"], price, original, discount,
                            gift["floor"], supply, sold, gift["image"], int(time.time())
                        ))
                    except Exception as e:
                        logger.error(f"Fragment scrape error for {gift['slug']}: {e}")
                await db.commit()
            finally:
                await db.close()
            await asyncio.sleep(FRAGMENT_SCRAPE_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Fragment worker error: {e}")
            await asyncio.sleep(FRAGMENT_SCRAPE_INTERVAL)


async def pro_expiry_worker():
    logger.info("PRO expiry worker started")
    while True:
        try:
            now = int(time.time())
            warning_threshold = now + 3 * 60
            db = await get_db()
            try:
                cursor = await db.execute("""
                    SELECT telegram_id, pro_expires_at
                    FROM users
                    WHERE is_pro = 1 AND pro_expires_at > 0 AND pro_expires_at <= ?
                """, (warning_threshold,))
                expiring = await cursor.fetchall()
                for user in expiring:
                    if user["pro_expires_at"] <= now:
                        await db.execute(
                            "UPDATE users SET is_pro = 0 WHERE telegram_id = ?",
                            (user["telegram_id"],)
                        )
                        try:
                            if aiogram_bot:
                                await aiogram_bot.send_message(
                                    user["telegram_id"],
                                    "Your PRO subscription has expired.\n"
                                    "Renew in the shop to keep all privileges!"
                                )
                        except Exception:
                            pass
                    else:
                        remaining = user["pro_expires_at"] - now
                        try:
                            if aiogram_bot:
                                await aiogram_bot.send_message(
                                    user["telegram_id"],
                                    f"PRO subscription expires in {remaining // 60} minutes.\n"
                                    f"Renew early to keep your privileges!"
                                )
                        except Exception:
                            pass
                await db.commit()
            finally:
                await db.close()
            await asyncio.sleep(PRO_CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"PRO expiry worker error: {e}")
            await asyncio.sleep(PRO_CHECK_INTERVAL)


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(title="NameHunter PRO & Cosmic Core", version="1.0.0")

background_tasks: list = []
aiogram_bot = None


@app.on_event("startup")
async def startup():
    await init_db()
    await init_telethon()

    background_tasks.append(asyncio.create_task(radar_monitor_worker()))
    background_tasks.append(asyncio.create_task(fragment_scrape_worker()))
    background_tasks.append(asyncio.create_task(pro_expiry_worker()))

    logger.info("All background workers started")


@app.on_event("shutdown")
async def shutdown():
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    if telethon_client and telethon_client.is_connected():
        await telethon_client.disconnect()
    logger.info("Shutdown complete")


# =============================================================================
# Static Files
# =============================================================================

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
if not (static_dir / "index.html").exists():
    root_index = Path("index.html")
    if root_index.exists():
        import shutil
        shutil.copy(str(root_index), str(static_dir / "index.html"))

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), media_type="text/html")
    return HTMLResponse("<h1>NameHunter PRO & Cosmic Core</h1><p>Place index.html in static/</p>")


# =============================================================================
# API: User Data
# =============================================================================

@app.get("/api/user")
async def get_user(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))
    if not telegram_id:
        raise HTTPException(status_code=401, detail="Invalid user")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()

        if not user:
            referral_code = f"ref_{telegram_id}"
            await db.execute("""
                INSERT INTO users
                (telegram_id, username, first_name, last_name, language_code,
                 referral_code, last_online_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                telegram_id,
                user_info.get("username", ""),
                user_info.get("first_name", ""),
                user_info.get("last_name", ""),
                user_info.get("language_code", "ru"),
                referral_code,
                now, now, now
            ))
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()

        if user["last_online_at"] and user["last_online_at"] > 0:
            offline_seconds = min(now - user["last_online_at"], OFFLINE_MINING_MAX_SECONDS)
            if offline_seconds > 60:
                offline_coins = int(offline_seconds * 0.5 * user["offline_income_level"])
                if offline_coins > 0:
                    await db.execute("""
                        UPDATE users
                        SET coins = coins + ?, total_coins_earned = total_coins_earned + ?,
                            last_online_at = ?, updated_at = ?
                        WHERE telegram_id = ?
                    """, (offline_coins, offline_coins, now, now, telegram_id))
                    await db.commit()
                    cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
                    user = await cursor.fetchone()

        if user["last_online_at"] and user["last_online_at"] > 0:
            elapsed = now - user["last_online_at"]
            energy_to_add = min(
                elapsed * user["energy_regen_rate"],
                user["max_energy"] - user["energy"]
            )
            if energy_to_add > 0:
                await db.execute("""
                    UPDATE users
                    SET energy = MIN(energy + ?, max_energy), last_online_at = ?, updated_at = ?
                    WHERE telegram_id = ?
                """, (int(energy_to_add), now, now, telegram_id))
                await db.commit()
                cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
                user = await cursor.fetchone()

        last_reset = user["last_generation_reset_at"] or 0
        if now - last_reset > 86400:
            await db.execute("""
                UPDATE users SET daily_generations_used = 0, last_generation_reset_at = ?
                WHERE telegram_id = ?
            """, (now, telegram_id))
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()

        last_spin = user["last_free_spin_at"] or 0
        if now - last_spin >= SPIN_COOLDOWN:
            await db.execute("""
                UPDATE users SET free_spins_available = MAX(free_spins_available, 1) WHERE telegram_id = ?
            """, (telegram_id,))
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()

        if user["is_pro"] and user["pro_expires_at"] and user["pro_expires_at"] <= now:
            await db.execute("UPDATE users SET is_pro = 0 WHERE telegram_id = ?", (telegram_id,))
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()

        if user["boost_expires_at"] and user["boost_expires_at"] <= now and user["boost_multiplier"] > 1.0:
            await db.execute(
                "UPDATE users SET boost_multiplier = 1.0 WHERE telegram_id = ?",
                (telegram_id,)
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()

        planet_idx = user["current_planet_index"]
        planet = PLANET_DATA[planet_idx] if planet_idx < len(PLANET_DATA) else PLANET_DATA[0]

        return {
            "telegram_id": user["telegram_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "is_pro": bool(user["is_pro"]),
            "pro_expires_at": user["pro_expires_at"],
            "coins": user["coins"],
            "energy": user["energy"],
            "max_energy": user["max_energy"],
            "energy_regen_rate": user["energy_regen_rate"],
            "multi_tap_level": user["multi_tap_level"],
            "offline_income_level": user["offline_income_level"],
            "total_clicks": user["total_clicks"],
            "total_coins_earned": user["total_coins_earned"],
            "current_planet_index": planet_idx,
            "planet_hp": user["planet_hp"],
            "planet_max_hp": user["planet_max_hp"],
            "planet_name": planet["name"],
            "planet_reward": planet["reward"],
            "planet_color": planet["color"],
            "referral_code": user["referral_code"],
            "free_spins_available": user["free_spins_available"],
            "daily_generations_used": user["daily_generations_used"],
            "free_generations_limit": FREE_DAILY_GENERATIONS if not user["is_pro"] else -1,
            "radar_slots": user["radar_slots"],
            "boost_multiplier": user["boost_multiplier"],
            "boost_expires_at": user["boost_expires_at"],
            "planets_destroyed": user["planets_destroyed"],
            "total_spins": user["total_spins"],
            "created_at": user["created_at"],
        }
    finally:
        await db.close()


# =============================================================================
# API: Clicker
# =============================================================================

@app.post("/api/click")
async def click(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    taps = min(int(body.get("taps", 1)), 50)

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        elapsed = now - user["last_online_at"] if user["last_online_at"] else 0
        energy_regen = min(elapsed * user["energy_regen_rate"], user["max_energy"] - user["energy"])
        current_energy = min(user["energy"] + int(energy_regen), user["max_energy"])

        taps_needed = min(taps, current_energy)
        if taps_needed <= 0:
            return {"success": False, "error": "No energy", "energy": current_energy, "max_energy": user["max_energy"]}

        multi_tap = user["multi_tap_level"]
        boost = user["boost_multiplier"] if user["boost_expires_at"] > now else 1.0
        damage_per_tap = multi_tap * boost
        total_damage = int(damage_per_tap * taps_needed)

        coins_earned = 0
        new_hp = user["planet_hp"] - total_damage
        new_planet_index = user["current_planet_index"]
        planet_destroyed = False

        if new_hp <= 0:
            planet_destroyed = True
            planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]
            coins_earned = planet["reward"]
            new_planet_index = (new_planet_index + 1) % len(PLANET_DATA)
            next_planet = PLANET_DATA[new_planet_index]
            new_hp = next_planet["hp"]
        else:
            planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]
            coins_earned = int(total_damage * planet["reward"] / planet["hp"])

        new_energy = current_energy - taps_needed

        await db.execute("""
            UPDATE users SET
                energy = ?,
                last_online_at = ?,
                last_click_at = ?,
                total_clicks = total_clicks + ?,
                coins = coins + ?,
                total_coins_earned = total_coins_earned + ?,
                planet_hp = ?,
                current_planet_index = ?,
                planets_destroyed = planets_destroyed + ?,
                updated_at = ?
            WHERE telegram_id = ?
        """, (
            new_energy, now, now, taps_needed,
            coins_earned, coins_earned,
            new_hp, new_planet_index,
            1 if planet_destroyed else 0,
            now, telegram_id
        ))

        await db.execute("""
            INSERT INTO leaderboard (user_id, total_clicks, total_coins, planets_destroyed, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                total_clicks = total_clicks + ?,
                total_coins = total_coins + ?,
                planets_destroyed = planets_destroyed + ?,
                updated_at = ?
        """, (
            telegram_id, taps_needed, coins_earned, 1 if planet_destroyed else 0, now,
            taps_needed, coins_earned, 1 if planet_destroyed else 0, now
        ))

        await db.commit()

        cursor = await db.execute(
            "SELECT total_clicks, total_coins_earned, planets_destroyed FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        stats = await cursor.fetchone()
        await check_achievements(db, telegram_id, stats)

        current_planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]

        return {
            "success": True,
            "coins_earned": coins_earned,
            "total_coins": user["total_coins_earned"] + coins_earned,
            "energy": new_energy,
            "max_energy": user["max_energy"],
            "planet_hp": new_hp,
            "planet_max_hp": current_planet["hp"],
            "planet_destroyed": planet_destroyed,
            "next_planet_index": new_planet_index,
            "planet_name": current_planet["name"],
            "planet_color": current_planet["color"],
            "damage": total_damage,
            "multi_tap": multi_tap,
            "boost": boost,
        }
    finally:
        await db.close()


# =============================================================================
# API: Username Check
# =============================================================================

@app.post("/api/check_username")
async def api_check_username(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    username = body.get("username", "").strip()
    category = body.get("category", "cosmos")

    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        last_reset = user["last_generation_reset_at"] or 0
        if now - last_reset > 86400:
            await db.execute("""
                UPDATE users SET daily_generations_used = 0, last_generation_reset_at = ?
                WHERE telegram_id = ?
            """, (now, telegram_id))
            await db.commit()
            user_generations = 0
        else:
            user_generations = user["daily_generations_used"]

        if not user["is_pro"] and user_generations >= FREE_DAILY_GENERATIONS:
            return {
                "success": False,
                "error": "Daily limit reached",
                "used": user_generations,
                "limit": FREE_DAILY_GENERATIONS,
                "is_pro": False,
            }

        result = await check_username_available(username)
        rarity = get_rarity()
        rarity_color = get_rarity_color(rarity)

        await db.execute("""
            INSERT INTO generation_history (user_id, username, category, rarity, is_available, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (telegram_id, username, category, rarity, 1 if result.get("available") else 0, now))

        await db.execute("""
            UPDATE users
            SET daily_generations_used = daily_generations_used + 1,
                total_generations = total_generations + 1,
                updated_at = ?
            WHERE telegram_id = ?
        """, (now, telegram_id))
        await db.commit()

        return {
            "success": True,
            "username": username,
            "available": result.get("available"),
            "method": result.get("method"),
            "rarity": rarity,
            "rarity_color": rarity_color,
            "category": NICKNAME_CATEGORIES.get(category, category),
            "generations_used": user_generations + 1,
            "generations_limit": FREE_DAILY_GENERATIONS if not user["is_pro"] else -1,
            "is_pro": user["is_pro"],
        }
    finally:
        await db.close()


@app.post("/api/generate")
async def api_generate(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    category = body.get("category", "cosmos")

    if category not in CATEGORY_WORDS:
        raise HTTPException(status_code=400, detail="Invalid category")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        last_reset = user["last_generation_reset_at"] or 0
        if now - last_reset > 86400:
            await db.execute("""
                UPDATE users SET daily_generations_used = 0, last_generation_reset_at = ?
                WHERE telegram_id = ?
            """, (now, telegram_id))
            await db.commit()
            user_generations = 0
        else:
            user_generations = user["daily_generations_used"]

        if not user["is_pro"] and user_generations >= FREE_DAILY_GENERATIONS:
            return {
                "success": False,
                "error": "Daily limit reached",
                "used": user_generations,
                "limit": FREE_DAILY_GENERATIONS,
            }

        suggestions = []
        for _ in range(10):
            nick = generate_nickname(category)
            rarity = get_rarity()
            suggestions.append({
                "username": nick,
                "rarity": rarity,
                "rarity_color": get_rarity_color(rarity),
            })

        await db.execute("""
            UPDATE users
            SET daily_generations_used = daily_generations_used + 1,
                total_generations = total_generations + 1,
                updated_at = ?
            WHERE telegram_id = ?
        """, (now, telegram_id))
        await db.commit()

        return {
            "success": True,
            "suggestions": suggestions,
            "category": NICKNAME_CATEGORIES.get(category, category),
            "generations_used": user_generations + 1,
            "generations_limit": FREE_DAILY_GENERATIONS if not user["is_pro"] else -1,
        }
    finally:
        await db.close()


# =============================================================================
# API: Quantum Wheel
# =============================================================================

@app.post("/api/spin")
async def api_spin(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    use_coin = body.get("use_coin", False)
    coin_cost = body.get("coin_cost", 5000)

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        free_spins = user["free_spins_available"]
        last_spin = user["last_free_spin_at"] or 0

        if now - last_spin >= SPIN_COOLDOWN:
            free_spins = max(free_spins, 1)

        if free_spins <= 0 and not use_coin:
            return {
                "success": False,
                "error": "No free spins available",
                "free_spins": free_spins,
                "next_spin_in": SPIN_COOLDOWN - (now - last_spin),
            }

        if use_coin and free_spins <= 0:
            if user["coins"] < coin_cost:
                return {
                    "success": False,
                    "error": "Not enough coins",
                    "coins": user["coins"],
                    "required": coin_cost,
                }
            await db.execute("""
                UPDATE users SET coins = coins - ?, updated_at = ? WHERE telegram_id = ?
            """, (coin_cost, now, telegram_id))

        if free_spins > 0:
            new_free_spins = free_spins - 1
            if new_free_spins == 0:
                await db.execute("""
                    UPDATE users
                    SET free_spins_available = ?, last_free_spin_at = ?, total_spins = total_spins + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                """, (new_free_spins, now, now, telegram_id))
            else:
                await db.execute("""
                    UPDATE users
                    SET free_spins_available = ?, total_spins = total_spins + 1, updated_at = ?
                    WHERE telegram_id = ?
                """, (new_free_spins, now, telegram_id))
        else:
            await db.execute("""
                UPDATE users SET total_spins = total_spins + 1, updated_at = ? WHERE telegram_id = ?
            """, (now, telegram_id))

        total_weight = sum(p["weight"] for p in SPIN_PRIZES)
        roll = random.uniform(0, total_weight)
        cumulative = 0
        selected = SPIN_PRIZES[-1]
        selected_index = len(SPIN_PRIZES) - 1
        for i, prize in enumerate(SPIN_PRIZES):
            cumulative += prize["weight"]
            if roll <= cumulative:
                selected = prize
                selected_index = i
                break

        reward_applied = False
        if selected["type"] == "coins":
            await db.execute("""
                UPDATE users
                SET coins = coins + ?, total_coins_earned = total_coins_earned + ?, updated_at = ?
                WHERE telegram_id = ?
            """, (selected["value"], selected["value"], now, telegram_id))
            reward_applied = True
        elif selected["type"] == "boost":
            boost_end = now + selected["value"] * 60
            await db.execute("""
                UPDATE users
                SET boost_multiplier = 2.0, boost_expires_at = ?, updated_at = ?
                WHERE telegram_id = ?
            """, (boost_end, now, telegram_id))
            reward_applied = True
        elif selected["type"] == "radar":
            await db.execute("""
                UPDATE users
                SET radar_slots = radar_slots + ?, updated_at = ?
                WHERE telegram_id = ?
            """, (selected["value"], now, telegram_id))
            reward_applied = True
        elif selected["type"] in ("pro_3d", "pro_7d", "pro_lifetime"):
            days = selected["value"]
            if days >= 9999:
                pro_end = now + 365 * 24 * 3600 * 10
            else:
                pro_end = now + days * 86400
            if user["is_pro"] and user["pro_expires_at"] > now:
                pro_end = user["pro_expires_at"] + (pro_end - now)
            await db.execute("""
                UPDATE users
                SET is_pro = 1, pro_expires_at = ?, pro_activated = MAX(pro_activated, 1),
                    radar_slots = MAX(radar_slots, 3), updated_at = ?
                WHERE telegram_id = ?
            """, (pro_end, now, telegram_id))
            reward_applied = True

        await db.commit()

        cursor = await db.execute("SELECT free_spins_available FROM users WHERE telegram_id = ?", (telegram_id,))
        updated = await cursor.fetchone()

        return {
            "success": True,
            "prize": {
                "type": selected["type"],
                "label": selected["label"],
                "value": selected["value"],
                "color": selected["color"],
                "index": selected_index,
            },
            "free_spins": updated["free_spins_available"],
            "reward_applied": reward_applied,
        }
    finally:
        await db.close()


# =============================================================================
# API: Radar
# =============================================================================

@app.get("/api/radar/targets")
async def get_radar_targets(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM radar_targets WHERE user_id = ? ORDER BY created_at DESC
        """, (telegram_id,))
        targets = await cursor.fetchall()
        return {
            "success": True,
            "targets": [
                {
                    "id": t["id"],
                    "username": t["target_username"],
                    "status": t["status"],
                    "notified_at": t["notified_at"],
                    "created_at": t["created_at"],
                }
                for t in targets
            ]
        }
    finally:
        await db.close()


@app.post("/api/radar/add")
async def add_radar_target(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    username = body.get("username", "").strip().lstrip("@")

    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT radar_slots FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM radar_targets WHERE user_id = ? AND status = 'watching'",
            (telegram_id,)
        )
        count = await cursor.fetchone()
        if count["cnt"] >= user["radar_slots"]:
            return {"success": False, "error": "Radar slots full", "slots_used": count["cnt"], "slots_max": user["radar_slots"]}

        await db.execute("""
            INSERT INTO radar_targets (user_id, target_username, status, created_at)
            VALUES (?, ?, 'watching', ?)
        """, (telegram_id, username, now))

        await db.execute("""
            UPDATE users SET radar_targets_count = radar_targets_count + 1, updated_at = ?
            WHERE telegram_id = ?
        """, (now, telegram_id))
        await db.commit()

        return {"success": True, "username": username, "status": "watching"}
    finally:
        await db.close()


@app.delete("/api/radar/{target_id}")
async def remove_radar_target(target_id: int, request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM radar_targets WHERE id = ? AND user_id = ?",
            (target_id, telegram_id)
        )
        await db.commit()
        return {"success": True}
    finally:
        await db.close()


# =============================================================================
# API: Fragment Tracker
# =============================================================================

@app.get("/api/fragment/offers")
async def get_fragment_offers(
    request: Request,
    min_discount: int = Query(0, ge=0, le=100),
    max_price: float = Query(100.0, ge=0),
):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM fragment_offers
            WHERE is_active = 1
              AND discount_percent >= ?
              AND price_ton <= ?
            ORDER BY discount_percent DESC
        """, (min_discount, max_price))
        offers = await cursor.fetchall()
        return {
            "success": True,
            "offers": [
                {
                    "id": o["id"],
                    "gift_name": o["gift_name"],
                    "gift_slug": o["gift_slug"],
                    "price_ton": o["price_ton"],
                    "original_price_ton": o["original_price_ton"],
                    "discount_percent": o["discount_percent"],
                    "floor_price_ton": o["floor_price_ton"],
                    "supply": o["supply"],
                    "sold": o["sold"],
                    "image_url": o["image_url"],
                    "scraped_at": o["scraped_at"],
                }
                for o in offers
            ]
        }
    finally:
        await db.close()


# =============================================================================
# API: History
# =============================================================================

@app.get("/api/history")
async def get_history(request: Request, limit: int = Query(50, ge=1, le=200)):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM generation_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (telegram_id, limit))
        history = await cursor.fetchall()
        return {
            "success": True,
            "history": [
                {
                    "id": h["id"],
                    "username": h["username"],
                    "category": h["category"],
                    "rarity": h["rarity"],
                    "is_available": bool(h["is_available"]),
                    "created_at": h["created_at"],
                }
                for h in history
            ]
        }
    finally:
        await db.close()


@app.delete("/api/history/{history_id}")
async def delete_history(history_id: int, request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM generation_history WHERE id = ? AND user_id = ?",
            (history_id, telegram_id)
        )
        await db.commit()
        return {"success": True}
    finally:
        await db.close()


# =============================================================================
# API: Achievements
# =============================================================================

async def check_achievements(db: aiosqlite.Connection, telegram_id: int, stats):
    now = int(time.time())
    for ach in ACHIEVEMENTS_DEFS:
        field_value = 0
        if ach["field"] == "total_clicks" and stats:
            field_value = stats["total_clicks"] or 0
        elif ach["field"] == "total_coins_earned" and stats:
            field_value = stats["total_coins_earned"] or 0
        elif ach["field"] == "planets_destroyed" and stats:
            field_value = stats["planets_destroyed"] or 0

        progress = min(field_value, ach["target"])

        cursor = await db.execute("""
            INSERT INTO achievements (user_id, achievement_key, progress, completed_at, claimed_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(user_id, achievement_key) DO UPDATE SET
                progress = MAX(progress, ?)
        """, (telegram_id, ach["key"], progress, now if progress >= ach["target"] else None, progress))
    await db.commit()


@app.get("/api/achievements")
async def get_achievements(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if user:
            await check_achievements(db, telegram_id, user)

        subqueries = " UNION ALL ".join(
            f"SELECT '{ach['key']}' as key, '{ach['name']}' as name, "
            f"'{ach['desc']}' as desc, {ach['target']} as target"
            for ach in ACHIEVEMENTS_DEFS
        )
        cursor = await db.execute(f"""
            SELECT a.*, ak.name, ak.desc, ak.target
            FROM achievements a
            JOIN ({subqueries}) ak ON a.achievement_key = ak.key
            WHERE a.user_id = ?
            ORDER BY a.completed_at DESC NULLS LAST, a.progress DESC
        """, (telegram_id,))
        achievements = await cursor.fetchall()

        return {
            "success": True,
            "achievements": [
                {
                    "key": a["achievement_key"],
                    "name": a["name"],
                    "desc": a["desc"],
                    "progress": a["progress"],
                    "target": a["target"],
                    "completed": a["completed_at"] is not None,
                    "claimed": a["claimed_at"] is not None,
                }
                for a in achievements
            ]
        }
    finally:
        await db.close()


@app.post("/api/achievements/{key}/claim")
async def claim_achievement(key: str, request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        now = int(time.time())
        ach_def = next((a for a in ACHIEVEMENTS_DEFS if a["key"] == key), None)
        if not ach_def:
            raise HTTPException(status_code=404, detail="Achievement not found")

        cursor = await db.execute("""
            SELECT * FROM achievements
            WHERE user_id = ? AND achievement_key = ? AND completed_at IS NOT NULL AND claimed_at IS NULL
        """, (telegram_id, key))
        ach = await cursor.fetchone()

        if not ach:
            return {"success": False, "error": "Not claimable"}

        reward = ach_def["target"] * 10
        await db.execute("""
            UPDATE achievements SET claimed_at = ? WHERE user_id = ? AND achievement_key = ?
        """, (now, telegram_id, key))

        await db.execute("""
            UPDATE users SET coins = coins + ?, total_coins_earned = total_coins_earned + ?, updated_at = ?
            WHERE telegram_id = ?
        """, (reward, reward, now, telegram_id))
        await db.commit()

        return {"success": True, "reward": reward, "key": key}
    finally:
        await db.close()


# =============================================================================
# API: Shop & Upgrades
# =============================================================================

@app.post("/api/shop/upgrade")
async def upgrade(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    upgrade_type = body.get("type", "")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        upgrade_costs = {
            "multitap": [500, 1500, 5000, 15000, 50000, 150000, 500000],
            "max_energy": [300, 900, 3000, 10000, 30000, 100000, 300000],
            "energy_regen": [200, 600, 2000, 6000, 20000, 60000, 200000],
            "offline_income": [1000, 3000, 10000, 30000, 100000, 300000, 1000000],
        }

        if upgrade_type not in upgrade_costs:
            raise HTTPException(status_code=400, detail="Invalid upgrade type")

        levels = upgrade_costs[upgrade_type]
        current_level = 0
        if upgrade_type == "multitap":
            current_level = user["multi_tap_level"] - 1
        elif upgrade_type == "max_energy":
            current_level = (user["max_energy"] - 1000) // 500
        elif upgrade_type == "energy_regen":
            current_level = (user["energy_regen_rate"] - 50) // 10
        elif upgrade_type == "offline_income":
            current_level = user["offline_income_level"] - 1

        if current_level >= len(levels):
            return {"success": False, "error": "Max level reached"}

        cost = levels[current_level]
        if user["coins"] < cost:
            return {"success": False, "error": "Not enough coins", "coins": user["coins"], "required": cost}

        await db.execute("UPDATE users SET coins = coins - ?, updated_at = ? WHERE telegram_id = ?", (cost, now, telegram_id))

        if upgrade_type == "multitap":
            new_val = user["multi_tap_level"] + 1
            await db.execute("UPDATE users SET multi_tap_level = ? WHERE telegram_id = ?", (new_val, telegram_id))
        elif upgrade_type == "max_energy":
            new_val = user["max_energy"] + 500
            await db.execute("""
                UPDATE users SET max_energy = ?, energy = ? WHERE telegram_id = ?
            """, (new_val, new_val, telegram_id))
        elif upgrade_type == "energy_regen":
            new_val = user["energy_regen_rate"] + 10
            await db.execute("UPDATE users SET energy_regen_rate = ? WHERE telegram_id = ?", (new_val, telegram_id))
        elif upgrade_type == "offline_income":
            new_val = user["offline_income_level"] + 1
            await db.execute("UPDATE users SET offline_income_level = ? WHERE telegram_id = ?", (new_val, telegram_id))

        await db.commit()
        return {"success": True, "type": upgrade_type, "cost": cost, "new_level": current_level + 2}
    finally:
        await db.close()


@app.post("/api/shop/pro")
async def purchase_pro(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    plan = body.get("plan", "")

    db = await get_db()
    try:
        now = int(time.time())
        pro_plans = {
            "3d": {"days": 3, "stars": 75},
            "7d": {"days": 7, "stars": 120},
            "1m": {"days": 30, "stars": 160},
            "life": {"days": 3650, "stars": 490},
        }

        if plan not in pro_plans:
            raise HTTPException(status_code=400, detail="Invalid plan")

        plan_data = pro_plans[plan]

        cursor = await db.execute("SELECT is_pro, pro_expires_at FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user["is_pro"] and user["pro_expires_at"] > now:
            pro_end = user["pro_expires_at"] + plan_data["days"] * 86400
        else:
            pro_end = now + plan_data["days"] * 86400

        await db.execute("""
            UPDATE users
            SET is_pro = 1, pro_expires_at = ?, pro_activated = MAX(pro_activated, 1),
                radar_slots = MAX(radar_slots, 3), updated_at = ?
            WHERE telegram_id = ?
        """, (pro_end, now, telegram_id))
        await db.commit()

        return {
            "success": True,
            "plan": plan,
            "stars": plan_data["stars"],
            "pro_expires_at": pro_end,
            "is_telegram_stars_payment": True,
        }
    finally:
        await db.close()


# =============================================================================
# API: Promo Codes
# =============================================================================

@app.post("/api/promo")
async def activate_promo(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    code = body.get("code", "").strip().upper()

    if not code:
        raise HTTPException(status_code=400, detail="Code required")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user["promo_locked_until"] and user["promo_locked_until"] > now:
            remaining = user["promo_locked_until"] - now
            return {
                "success": False,
                "error": f"Brute-force protection: locked for {remaining // 60}m {remaining % 60}s",
                "locked_until": user["promo_locked_until"],
            }

        cursor = await db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,))
        promo = await cursor.fetchone()

        if not promo or not promo["is_active"]:
            attempts = user["promo_attempts"] + 1
            locked_until = 0
            if attempts >= 5:
                locked_until = now + 300
                attempts = 0
            await db.execute("""
                UPDATE users SET promo_attempts = ?, promo_locked_until = ?, updated_at = ?
                WHERE telegram_id = ?
            """, (attempts, locked_until, now, telegram_id))
            await db.commit()
            if locked_until:
                return {"success": False, "error": "Too many attempts. Locked for 5 minutes."}
            return {"success": False, "error": "Invalid promo code", "attempts_left": 5 - attempts}

        if promo["expires_at"] and promo["expires_at"] < now:
            return {"success": False, "error": "Promo code expired"}

        if promo["used_count"] >= promo["max_uses"]:
            return {"success": False, "error": "Promo code fully redeemed"}

        reward_type = promo["reward_type"]
        reward_value = promo["reward_value"]

        await db.execute("""
            UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?
        """, (code,))

        if reward_type == "coins":
            await db.execute("""
                UPDATE users
                SET coins = coins + ?, total_coins_earned = total_coins_earned + ?,
                    promo_attempts = 0, updated_at = ?
                WHERE telegram_id = ?
            """, (reward_value, reward_value, now, telegram_id))
        elif reward_type == "pro_days":
            pro_end = now + reward_value * 86400
            if user["is_pro"] and user["pro_expires_at"] > now:
                pro_end = user["pro_expires_at"] + reward_value * 86400
            await db.execute("""
                UPDATE users
                SET is_pro = 1, pro_expires_at = ?, pro_activated = MAX(pro_activated, 1),
                    promo_attempts = 0, updated_at = ?
                WHERE telegram_id = ?
            """, (pro_end, now, telegram_id))
        elif reward_type == "spins":
            await db.execute("""
                UPDATE users
                SET free_spins_available = free_spins_available + ?,
                    promo_attempts = 0, updated_at = ?
                WHERE telegram_id = ?
            """, (reward_value, now, telegram_id))
        elif reward_type == "energy":
            await db.execute("""
                UPDATE users
                SET energy = MIN(energy + ?, max_energy),
                    promo_attempts = 0, updated_at = ?
                WHERE telegram_id = ?
            """, (reward_value, now, telegram_id))

        await db.commit()
        return {"success": True, "reward_type": reward_type, "reward_value": reward_value}
    finally:
        await db.close()


# =============================================================================
# API: Referrals
# =============================================================================

@app.get("/api/referrals")
async def get_referrals(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("SELECT referral_code FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        referral_code = user["referral_code"]
        referral_link = f"https://t.me/{BOT_USERNAME}?start={referral_code}"

        cursor = await db.execute("""
            SELECT telegram_id, username, first_name, created_at
            FROM users WHERE referred_by = ?
            ORDER BY created_at DESC
        """, (telegram_id,))
        referrals = await cursor.fetchall()

        return {
            "success": True,
            "referral_code": referral_code,
            "referral_link": referral_link,
            "total_referrals": len(referrals),
            "referrals": [
                {
                    "telegram_id": r["telegram_id"],
                    "username": r["username"],
                    "first_name": r["first_name"],
                    "joined_at": r["created_at"],
                }
                for r in referrals
            ]
        }
    finally:
        await db.close()


@app.post("/api/referrals/apply")
async def apply_referral(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    ref_code = body.get("ref_code", "")

    if not ref_code:
        raise HTTPException(status_code=400, detail="Ref code required")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user["referred_by"]:
            return {"success": False, "error": "Already referred"}

        if user["referral_code"] == ref_code:
            return {"success": False, "error": "Cannot refer yourself"}

        cursor = await db.execute("SELECT telegram_id FROM users WHERE referral_code = ?", (ref_code,))
        referrer = await cursor.fetchone()
        if not referrer:
            return {"success": False, "error": "Invalid referral code"}

        await db.execute("UPDATE users SET referred_by = ? WHERE telegram_id = ?", (referrer["telegram_id"], telegram_id))
        await db.commit()

        return {"success": True, "referrer_id": referrer["telegram_id"]}
    finally:
        await db.close()


# =============================================================================
# API: Leaderboard
# =============================================================================

@app.get("/api/leaderboard")
async def get_leaderboard(request: Request, sort: str = Query("clicks")):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    db = await get_db()
    try:
        order_col = "total_clicks"
        if sort == "coins":
            order_col = "total_coins"
        elif sort == "planets":
            order_col = "planets_destroyed"
        elif sort == "referrals":
            order_col = "referrals_count"

        cursor = await db.execute(f"""
            SELECT l.*, u.username, u.first_name, u.is_pro
            FROM leaderboard l
            JOIN users u ON u.telegram_id = l.user_id
            ORDER BY l.{order_col} DESC
            LIMIT 100
        """)
        entries = await cursor.fetchall()

        return {
            "success": True,
            "leaderboard": [
                {
                    "rank": i + 1,
                    "user_id": e["user_id"],
                    "username": e["username"],
                    "first_name": e["first_name"],
                    "is_pro": bool(e["is_pro"]),
                    "total_clicks": e["total_clicks"],
                    "total_coins": e["total_coins"],
                    "planets_destroyed": e["planets_destroyed"],
                    "referrals_count": e["referrals_count"],
                }
                for i, e in enumerate(entries)
            ]
        }
    finally:
        await db.close()


# =============================================================================
# API: Galactic Raid
# =============================================================================

@app.get("/api/raid/current")
async def get_current_raid(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM galactic_raids WHERE status = 'active' ORDER BY started_at DESC LIMIT 1
        """)
        raid = await cursor.fetchone()
        if not raid:
            return {"success": True, "raid": None}

        return {
            "success": True,
            "raid": {
                "id": raid["id"],
                "boss_planet_index": raid["boss_planet_index"],
                "boss_hp": raid["boss_hp"],
                "boss_max_hp": raid["boss_max_hp"],
                "total_damage": raid["total_damage"],
                "ends_at": raid["ends_at"],
                "started_at": raid["started_at"],
            }
        }
    finally:
        await db.close()


@app.post("/api/raid/damage")
async def raid_damage(request: Request):
    if not ENABLE_GALACTIC_RAID:
        raise HTTPException(status_code=403, detail="Feature disabled")

    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    damage = min(int(body.get("damage", 0)), 1000)

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("""
            SELECT * FROM galactic_raids WHERE status = 'active' ORDER BY started_at DESC LIMIT 1
        """)
        raid = await cursor.fetchone()
        if not raid:
            return {"success": False, "error": "No active raid"}

        new_hp = max(raid["boss_hp"] - damage, 0)
        await db.execute("""
            UPDATE galactic_raids SET boss_hp = ?, total_damage = total_damage + ? WHERE id = ?
        """, (new_hp, damage, raid["id"]))

        await db.execute("""
            INSERT INTO raid_participants (raid_id, user_id, damage_dealt)
            VALUES (?, ?, ?)
            ON CONFLICT(raid_id, user_id) DO UPDATE SET
                damage_dealt = damage_dealt + ?
        """, (raid["id"], telegram_id, damage, damage))

        if new_hp <= 0:
            await db.execute("""
                UPDATE galactic_raids SET status = 'completed', boss_hp = 0 WHERE id = ?
            """, (raid["id"],))
            participants = await db.execute_fetchall("""
                SELECT user_id, damage_dealt FROM raid_participants WHERE raid_id = ?
            """, (raid["id"],))
            total_damage = raid["boss_max_hp"]
            for p in participants:
                share = p["damage_dealt"] / total_damage if total_damage > 0 else 0
                reward = int(PLANET_DATA[raid["boss_planet_index"]]["reward"] * 10 * share)
                if reward > 0:
                    await db.execute("""
                        UPDATE users SET coins = coins + ?, total_coins_earned = total_coins_earned + ?
                        WHERE telegram_id = ?
                    """, (reward, reward, p["user_id"]))

        await db.commit()

        return {
            "success": True,
            "damage_dealt": damage,
            "boss_hp": new_hp,
            "boss_max_hp": raid["boss_max_hp"],
            "raid_completed": new_hp <= 0,
        }
    finally:
        await db.close()


# =============================================================================
# API: Time Capsule
# =============================================================================

@app.post("/api/capsule/create")
async def create_capsule(request: Request):
    if not ENABLE_TIME_CAPSULE:
        raise HTTPException(status_code=403, detail="Feature disabled")

    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    username = body.get("username", "").strip()
    category = body.get("category", "cosmos")
    hours = min(int(body.get("hours", 24)), 72)

    db = await get_db()
    try:
        now = int(time.time())
        rarity = get_rarity()
        rarity_multiplier = {"SSS+": 50, "SS": 20, "S": 5, "A": 1}
        cost = int(hours * 100 * rarity_multiplier.get(rarity, 1))

        cursor = await db.execute("SELECT coins FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["coins"] < cost:
            return {"success": False, "error": "Not enough coins", "required": cost}

        await db.execute("UPDATE users SET coins = coins - ? WHERE telegram_id = ?", (cost, telegram_id))
        await db.execute("""
            INSERT INTO time_capsules (user_id, username, category, rarity, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (telegram_id, username, category, rarity, now + hours * 3600, now))
        await db.commit()

        return {
            "success": True,
            "capsule": {
                "username": username,
                "rarity": rarity,
                "expires_at": now + hours * 3600,
                "cost": cost,
            }
        }
    finally:
        await db.close()


@app.get("/api/capsule/list")
async def list_capsules(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM time_capsules WHERE user_id = ? ORDER BY expires_at DESC
        """, (telegram_id,))
        capsules = await cursor.fetchall()
        now = int(time.time())
        return {
            "success": True,
            "capsules": [
                {
                    "id": c["id"],
                    "username": c["username"],
                    "category": c["category"],
                    "rarity": c["rarity"],
                    "expires_at": c["expires_at"],
                    "is_expired": c["expires_at"] <= now,
                    "is_claimed": bool(c["is_claimed"]),
                    "created_at": c["created_at"],
                }
                for c in capsules
            ]
        }
    finally:
        await db.close()


# =============================================================================
# API: Quantum Intercept
# =============================================================================

@app.post("/api/quantum/intercept")
async def quantum_intercept(request: Request):
    if not ENABLE_QUANTUM_INTERCEPT:
        raise HTTPException(status_code=403, detail="Feature disabled")

    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    username = body.get("username", "").strip().lstrip("@")

    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    db = await get_db()
    try:
        now = int(time.time())
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        cost = 50000
        if user["coins"] < cost:
            return {"success": False, "error": "Not enough coins", "required": cost}

        check = await check_username_available(username)
        if check.get("available") is not True:
            return {"success": False, "error": "Username is not available"}

        await db.execute("UPDATE users SET coins = coins - ? WHERE telegram_id = ?", (cost, telegram_id))
        await db.execute("""
            INSERT INTO quantum_intercepts (user_id, target_username, status, created_at)
            VALUES (?, ?, 'pending', ?)
        """, (telegram_id, username, now))
        await db.commit()

        return {
            "success": True,
            "username": username,
            "cost": cost,
            "status": "pending",
            "message": f"Quantum trap set for @{username}. Awaiting Fragment purchase..."
        }
    finally:
        await db.close()


# =============================================================================
# API: User Settings
# =============================================================================

@app.post("/api/user/settings")
async def update_settings(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    body = await request.json()
    db = await get_db()
    try:
        now = int(time.time())
        updates = []
        params = []

        if "language_code" in body:
            updates.append("language_code = ?")
            params.append(body["language_code"][:10])

        if updates:
            updates.append("updated_at = ?")
            params.append(now)
            params.append(telegram_id)
            await db.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE telegram_id = ?",
                params
            )
            await db.commit()

        return {"success": True}
    finally:
        await db.close()


# =============================================================================
# API: Coins Info
# =============================================================================

@app.get("/api/coins")
async def get_coins(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_info = json.loads(data.get("user", "{}"))
    telegram_id = int(user_info.get("id", 0))

    db = await get_db()
    try:
        cursor = await db.execute("SELECT coins, total_coins_earned FROM users WHERE telegram_id = ?", (telegram_id,))
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, "coins": user["coins"], "total_earned": user["total_coins_earned"]}
    finally:
        await db.close()


# =============================================================================
# WebSocket: Real-time Clicker Sync
# =============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        async with self._lock:
            if user_id not in self.active_connections:
                self.active_connections[user_id] = []
            self.active_connections[user_id].append(websocket)

    async def disconnect(self, websocket: WebSocket, user_id: int):
        async with self._lock:
            if user_id in self.active_connections:
                if websocket in self.active_connections[user_id]:
                    self.active_connections[user_id].remove(websocket)
                if not self.active_connections[user_id]:
                    del self.active_connections[user_id]

    async def send_to_user(self, user_id: int, message: dict):
        async with self._lock:
            connections = self.active_connections.get(user_id, [])[:]
        for conn in connections:
            try:
                await conn.send_json(message)
            except Exception:
                pass

    async def broadcast(self, message: dict):
        async with self._lock:
            all_conns = []
            for conns in self.active_connections.values():
                all_conns.extend(conns)
        for conn in all_conns:
            try:
                await conn.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = None
    try:
        auth_msg = await websocket.receive_json()
        init_data = auth_msg.get("init_data", "")
        data = validate_init_data(init_data)
        if not data:
            await websocket.send_json({"type": "error", "message": "Unauthorized"})
            await websocket.close()
            return

        user_info = json.loads(data.get("user", "{}"))
        user_id = int(user_info.get("id", 0))

        if not user_id:
            await websocket.send_json({"type": "error", "message": "Invalid user"})
            await websocket.close()
            return

        await ws_manager.connect(websocket, user_id)
        await websocket.send_json({"type": "connected", "user_id": user_id})

        online_count = len(ws_manager.active_connections)
        await ws_manager.broadcast({"type": "online_count", "count": online_count})

        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "ts": int(time.time())})

            elif msg_type == "click_batch":
                taps = min(int(msg.get("taps", 1)), 50)
                db = await get_db()
                try:
                    now = int(time.time())
                    cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
                    user = await cursor.fetchone()
                    if not user:
                        continue

                    elapsed = now - user["last_online_at"] if user["last_online_at"] else 0
                    energy_regen = min(elapsed * user["energy_regen_rate"], user["max_energy"] - user["energy"])
                    current_energy = min(user["energy"] + int(energy_regen), user["max_energy"])

                    taps_needed = min(taps, current_energy)
                    if taps_needed <= 0:
                        await websocket.send_json({"type": "no_energy", "energy": current_energy})
                        continue

                    multi_tap = user["multi_tap_level"]
                    boost = user["boost_multiplier"] if user["boost_expires_at"] > now else 1.0
                    damage_per_tap = multi_tap * boost
                    total_damage = int(damage_per_tap * taps_needed)

                    coins_earned = 0
                    new_hp = user["planet_hp"] - total_damage
                    new_planet_index = user["current_planet_index"]
                    planet_destroyed = False

                    if new_hp <= 0:
                        planet_destroyed = True
                        planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]
                        coins_earned = planet["reward"]
                        new_planet_index = (new_planet_index + 1) % len(PLANET_DATA)
                        next_planet = PLANET_DATA[new_planet_index]
                        new_hp = next_planet["hp"]
                    else:
                        planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]
                        coins_earned = int(total_damage * planet["reward"] / planet["hp"])

                    new_energy = current_energy - taps_needed

                    await db.execute("""
                        UPDATE users SET
                            energy = ?, last_online_at = ?, last_click_at = ?,
                            total_clicks = total_clicks + ?, coins = coins + ?,
                            total_coins_earned = total_coins_earned + ?,
                            planet_hp = ?, current_planet_index = ?,
                            planets_destroyed = planets_destroyed + ?, updated_at = ?
                        WHERE telegram_id = ?
                    """, (
                        new_energy, now, now, taps_needed, coins_earned, coins_earned,
                        new_hp, new_planet_index,
                        1 if planet_destroyed else 0, now, user_id
                    ))

                    await db.execute("""
                        INSERT INTO leaderboard (user_id, total_clicks, total_coins, planets_destroyed, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            total_clicks = total_clicks + ?,
                            total_coins = total_coins + ?,
                            planets_destroyed = planets_destroyed + ?,
                            updated_at = ?
                    """, (
                        user_id, taps_needed, coins_earned, 1 if planet_destroyed else 0, now,
                        taps_needed, coins_earned, 1 if planet_destroyed else 0, now
                    ))

                    await db.commit()

                    cursor = await db.execute(
                        "SELECT total_clicks, total_coins_earned, planets_destroyed FROM users WHERE telegram_id = ?",
                        (user_id,)
                    )
                    stats = await cursor.fetchone()
                    await check_achievements(db, user_id, stats)

                    current_planet = PLANET_DATA[new_planet_index] if new_planet_index < len(PLANET_DATA) else PLANET_DATA[0]

                    await websocket.send_json({
                        "type": "click_result",
                        "coins_earned": coins_earned,
                        "total_coins": stats["total_coins_earned"],
                        "energy": new_energy,
                        "max_energy": user["max_energy"],
                        "planet_hp": new_hp,
                        "planet_max_hp": current_planet["hp"],
                        "planet_destroyed": planet_destroyed,
                        "next_planet_index": new_planet_index,
                        "planet_name": current_planet["name"],
                        "planet_color": current_planet["color"],
                        "damage": total_damage,
                        "multi_tap": multi_tap,
                        "boost": boost,
                    })

                finally:
                    await db.close()

            elif msg_type == "get_online":
                online_count = len(ws_manager.active_connections)
                await websocket.send_json({"type": "online_count", "count": online_count})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if user_id:
            await ws_manager.disconnect(websocket, user_id)
            online_count = len(ws_manager.active_connections)
            try:
                await ws_manager.broadcast({"type": "online_count", "count": online_count})
            except Exception:
                pass


# =============================================================================
# Telegram Bot (Aiogram)
# =============================================================================

async def setup_bot():
    global aiogram_bot
    if not BOT_TOKEN:
        logger.info("BOT_TOKEN not set, Telegram bot disabled")
        return

    try:
        from aiogram import Bot, Dispatcher, Router, F
        from aiogram.types import (
            Message, InlineKeyboardMarkup, InlineKeyboardButton,
            WebAppInfo, CallbackQuery
        )
        from aiogram.filters import CommandStart, Command

        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()
        router = Router()
        dp.include_router(router)
        aiogram_bot = bot

        @router.message(CommandStart())
        async def cmd_start(message: Message):
            args = message.text.split(maxsplit=1)
            ref_code = args[1] if len(args) > 1 else None

            db = await get_db()
            try:
                now = int(time.time())
                telegram_id = message.from_user.id
                cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
                user = await cursor.fetchone()

                if not user:
                    my_ref_code = f"ref_{telegram_id}"
                    referred_by = None
                    if ref_code and ref_code.startswith("ref_"):
                        try:
                            referrer_id = int(ref_code.replace("ref_", ""))
                            cursor2 = await db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (referrer_id,))
                            referrer = await cursor2.fetchone()
                            if referrer and referrer_id != telegram_id:
                                referred_by = referrer_id
                        except (ValueError, IndexError):
                            pass

                    await db.execute("""
                        INSERT INTO users
                        (telegram_id, username, first_name, last_name, language_code,
                         referral_code, referred_by, last_online_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        telegram_id,
                        message.from_user.username or "",
                        message.from_user.first_name or "",
                        message.from_user.last_name or "",
                        message.from_user.language_code or "ru",
                        my_ref_code,
                        referred_by,
                        now, now, now
                    ))
                    await db.commit()

                    if referred_by:
                        await db.execute("""
                            UPDATE users SET referral_spins_earned = referral_spins_earned + 1
                            WHERE telegram_id = ?
                        """, (referred_by,))
                        await db.commit()
                        try:
                            await bot.send_message(
                                referred_by,
                                f"New referral: {message.from_user.first_name}!\n"
                                f"+1 free spin earned!"
                            )
                        except Exception:
                            pass

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="Launch NameHunter",
                        web_app=WebAppInfo(url=WEBAPP_URL)
                    )]
                ])

                await message.answer(
                    f"NameHunter PRO & Cosmic Core\n\n"
                    f"Welcome, {message.from_user.first_name}!\n\n"
                    f"Destroy planets, earn coins\n"
                    f"Check usernames for availability\n"
                    f"Spin the reward wheel\n"
                    f"Track usernames with Radar\n\n"
                    f"Press the button below to start!",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            finally:
                await db.close()

        @router.message(Command("help"))
        async def cmd_help(message: Message):
            await message.answer(
                "NameHunter PRO & Cosmic Core\n\n"
                "/start - Launch WebApp\n"
                "/help - Help\n"
                "/ref - Get referral link\n"
                "/status - Account status",
                parse_mode="Markdown",
            )

        @router.message(Command("ref"))
        async def cmd_ref(message: Message):
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT referral_code FROM users WHERE telegram_id = ?",
                    (message.from_user.id,)
                )
                user = await cursor.fetchone()
                if not user:
                    await message.answer("Please run /start first")
                    return
                link = f"https://t.me/{BOT_USERNAME}?start={user['referral_code']}"
                await message.answer(
                    f"Your referral link:\n\n{link}\n\n"
                    f"Share with friends and get +1 free spin for each!",
                    parse_mode="Markdown",
                )
            finally:
                await db.close()

        @router.message(Command("status"))
        async def cmd_status(message: Message):
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT coins, total_clicks, planets_destroyed, is_pro, pro_expires_at FROM users WHERE telegram_id = ?",
                    (message.from_user.id,)
                )
                user = await cursor.fetchone()
                if not user:
                    await message.answer("Please run /start first")
                    return
                pro_status = "PRO" if user["is_pro"] else "Free"
                if user["is_pro"] and user["pro_expires_at"]:
                    expires = datetime.fromtimestamp(user["pro_expires_at"])
                    pro_status += f" (until {expires.strftime('%d.%m.%Y %H:%M')})"
                await message.answer(
                    f"Account Status:\n\n"
                    f"Coins: {user['coins']:,}\n"
                    f"Total taps: {user['total_clicks']:,}\n"
                    f"Planets destroyed: {user['planets_destroyed']}\n"
                    f"Status: {pro_status}",
                    parse_mode="Markdown",
                )
            finally:
                await db.close()

        @router.callback_query(F.data == "open_app")
        async def cb_open_app(callback: CallbackQuery):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="Launch NameHunter",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )]
            ])
            await callback.message.answer(
                "Tap to open the app:",
                reply_markup=keyboard,
            )
            await callback.answer()

        dp["bot"] = bot

        async def on_startup(*args, **kwargs):
            logger.info("Aiogram bot polling started")
            asyncio.create_task(dp.start_polling(bot))

        dp.start_polling = on_startup

        await dp.start_polling(bot)

    except Exception as e:
        logger.error(f"Bot setup failed: {e}")


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    os.makedirs("data", exist_ok=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if BOT_TOKEN:
        loop.create_task(setup_bot())

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=True,
    )
