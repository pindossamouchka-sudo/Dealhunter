#!/bin/bash
set -euo pipefail

# =============================================================================
# NameHunter PRO & Cosmic Core — Production Deploy Script
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +'%H:%M:%S')]${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
error() { echo -e "${RED}✗${NC} $*"; exit 1; }

# =============================================================================
# Pre-flight Checks
# =============================================================================
log "Running pre-flight checks..."

[[ -f ".env" ]] || error "Missing .env file. Copy from .env.example and configure."
[[ -f "requirements.txt" ]] || error "Missing requirements.txt"
[[ -f "main.py" ]] || error "Missing main.py"
[[ -f "index.html" ]] || error "Missing index.html (frontend)"

command -v python3 >/dev/null || error "Python 3 not found"
command -v pip3 >/dev/null || error "pip3 not found"

python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'" || error "Python 3.11+ required"

success "Pre-flight checks passed"

# =============================================================================
# Environment Setup
# =============================================================================
log "Setting up Python environment..."

if [[ ! -d "venv" ]]; then
    python3 -m venv venv
    success "Virtual environment created"
fi

source venv/bin/activate
pip install --upgrade pip wheel setuptools >/dev/null 2>&1
pip install -r requirements.txt >/dev/null 2>&1
success "Dependencies installed"

# =============================================================================
# Database Migration
# =============================================================================
log "Running database migrations..."
mkdir -p data
python3 -c "
import asyncio, aiosqlite, os
async def migrate():
    db_path = os.getenv('DATABASE_PATH', './data/namehunter.db')
    async with aiosqlite.connect(db_path) as db:
        await db.execute('PRAGMA journal_mode=WAL')
        await db.execute('PRAGMA synchronous=NORMAL')
        await db.execute('PRAGMA cache_size=-32768')
        
        await db.execute('''
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
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_referral ON users(referral_code)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_referred ON users(referred_by)')
        
        await db.execute('''
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
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS radar_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_username TEXT NOT NULL,
                status TEXT DEFAULT 'watching',
                notified_at INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_radar_user ON radar_targets(user_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_radar_status ON radar_targets(status)')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id INTEGER PRIMARY KEY,
                total_clicks INTEGER DEFAULT 0,
                total_coins INTEGER DEFAULT 0,
                planets_destroyed INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        ''')
        
        await db.execute('''
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
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_history_user ON generation_history(user_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_history_created ON generation_history(created_at)')
        
        await db.execute('''
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
        ''')
        
        await db.execute('''
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
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_fragment_active ON fragment_offers(is_active)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_fragment_discount ON fragment_offers(discount_percent)')
        
        await db.execute('''
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
        ''')
        
        await db.execute('''
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
        ''')
        
        await db.execute('''
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
        ''')
        
        await db.execute('''
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
        ''')
        
        await db.commit()
        print('Migrations completed')
asyncio.run(migrate())
"
success "Database migrated"

# =============================================================================
# Static Files & Frontend
# =============================================================================
log "Preparing static assets..."
mkdir -p static
# index.html should already be in project root, copy to static for serving
cp index.html static/ 2>/dev/null || true
success "Static assets ready"

# =============================================================================
# Systemd Service (if on Linux)
# =============================================================================
if [[ "$OSTYPE" == "linux-gnu"* ]] && [[ -w /etc/systemd/system/ ]]; then
    log "Installing systemd service..."
    cat > /etc/systemd/system/namehunter.service <<EOF
[Unit]
Description=NameHunter PRO & Cosmic Core
After=network.target

[Service]
Type=exec
User=$USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
ExecStart=$PROJECT_DIR/venv/bin/python main.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable namehunter >/dev/null 2>&1
    success "Systemd service installed"
fi

# =============================================================================
# Health Check
# =============================================================================
log "Starting health check..."
timeout 10 python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
from main import app
print('Import OK')
" || error "Application import failed"

success "Health check passed"

# =============================================================================
# Done
# =============================================================================
echo
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Deploy completed successfully!                          ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  Run: source venv/bin/activate && python main.py         ║${NC}"
echo -e "${GREEN}║  Or:  systemctl start namehunter (Linux)                 ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"