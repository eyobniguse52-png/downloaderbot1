import logging
import os
import tempfile
import json
import yt_dlp  # CRITICAL: Was missing in original code
from pathlib import Path
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
import asyncio
from asyncio import Lock

# Load config safely
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip())

# ⚠️ WARNING: /tmp is EPHEMERAL on Railway! Approvals reset on restart.
APPROVED_FILE = "/tmp/approved_users.json"
ALLOWED_DOMAINS = ["instagram.com"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Thread-safe access to approved users
approved_users_lock = Lock()

def load_approved():
    if Path(APPROVED_FILE).exists():
        try:
            with open(APPROVED_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading approved users: {e}")
            return set()
    return set()

def save_approved(users):
    try:
        with open(APPROVED_FILE, "w") as f:
            json.dump(list(users), f)
    except Exception as e:
        logger.error(f"Error saving approved users: {e}")

approved_users = load_approved()

def is_allowed_url(url: str) -> bool:
    """Strict URL validation"""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().removeprefix("www.")
        return any(netloc.endswith(domain) for domain in ALLOWED_DOMAINS)
    except Exception:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with approved_users_lock:
        if user_id in approved_users:
            await update.message.reply_text(
                "✨ Send me an Instagram video/reel link to download!\n\n"
                "Example: https://www.instagram.com/reel/ABC123/"
            )
        else:
            await update.message.reply_text(
                "👋 Welcome! Send an Instagram link to request access.\n\n"
                "⚠️ Note: Approvals reset after server restarts"
            )

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Handle commands
    if url.startswith("/"):
        await start(update, context)
        return

    async with approved_users_lock:
        if user_id not in approved_users:
            await update.message.reply_text("📩 Request sent to admin for approval.")
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton("❌ Ignore", callback_data="ignore")
            ]])
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=f"🔔 New user request:\nID: `{user_id}`\nUsername: @{update.effective_user.username or 'N/A'}",
                    reply_markup=btn,
                    parse_mode="MarkdownV2"
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")
                await update.message.reply_text("❌ Failed to send request. Please try again later.")
            return

    # URL validation
    if not is_allowed_url(url):
        await update.message.reply_text(
            "⚠️ Please send a valid Instagram link\n\n"
            "✅ Works: https://instagram.com/reel/ABC123/\n"
            "❌ Won't work: tiktok.com, facebook.com"
        )
        return

    status_msg = await update.message.reply_text("⏳ Downloading video...")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ydl_opts = {
                'outtmpl': os.path.join(tmp_dir, '%(title).50s.%(ext)s'),
                'format': 'best[height<=720][filesize<50M]/best[height<=720]',
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 20,
                'extractor_retries': 2,
                'fragment_retries': 2,
                'skip_download': False,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_path = ydl.prepare_filename(info)
            
            # Fallback file detection
            if not os.path.exists(video_path):
                files = [f for f in Path(tmp_dir).glob("*") if f.is_file()]
                if not files:
                    raise FileNotFoundError("No video file downloaded")
                video_path = str(files[0])
            
            # Telegram size limit check (50 MB)
            file_size = os.path.getsize(video_path)
            if file_size > 50 * 1024 * 1024:
                await status_msg.edit_text(
                    "❌ Video too large (>50 MB)\n\n"
                    "Instagram videos longer than ~1 minute often exceed Telegram's limit."
                )
                return
            
            # Send video
            await status_msg.edit_text("📤 Uploading to Telegram...")
            with open(video_path, 'rb') as f:
                await update.message.reply_video(
                    video=f,
                    caption="✅ Downloaded!",
                    supports_streaming=True
                )
            await status_msg.delete()
            
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e).lower()
        if "private" in error_msg or "login" in error_msg:
            msg = "❌ Private content not supported\n\nUse public Instagram posts/reels only."
        elif "age" in error_msg or "restricted" in error_msg:
            msg = "❌ Age-restricted content not supported"
        elif "404" in error_msg or "not found" in error_msg:
            msg = "❌ Invalid or expired link\n\nCheck the URL and try again."
        else:
            msg = "❌ Download failed\n\nTry a different public Instagram video."
        await status_msg.edit_text(msg)
        logger.warning(f"Download error for {url}: {str(e)[:150]}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Unexpected error occurred. Please try again later.")

async def handle_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_USER_ID:
        await query.edit_message_text("🚫 Admin access only")
        return
    
    data = query.data
    if data.startswith("approve_"):
        uid = int(data.split("_")[1])
        async with approved_users_lock:
            approved_users.add(uid)
            save_approved(approved_users)
        await query.edit_message_text(f"✅ Approved user {uid}!")
        try:
            await context.bot.send_message(
                uid, 
                "🎉 You've been approved!\n\nSend any Instagram video/reel link to download."
            )
        except Exception as e:
            logger.warning(f"Could not notify user {uid}: {e}")
    else:  # "ignore"
        await query.edit_message_text("🔕 Ignored")

async def main():
    if not BOT_TOKEN or ADMIN_USER_ID == 0:
        logger.error("❌ Missing BOT_TOKEN or ADMIN_USER_ID")
        logger.error("Set them in Railway Variables tab")
        return

    logger.info(f"🚀 Starting bot | Admin ID: {ADMIN_USER_ID}")
    
    # Persistence warning
    async with approved_users_lock:
        count = len(approved_users)
    logger.info(f"✅ Loaded {count} approved users")
    if count == 0:
        logger.warning(
            "⚠️ APPROVALS ARE EPHEMERAL! They reset when Railway restarts the container.\n"
            "💡 Fix later: Add PostgreSQL plugin for persistent storage"
        )

    app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.add_handler(CallbackQueryHandler(handle_btn))
    
    # Start bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    logger.info("✅ Bot is running on Railway!")
    logger.info("💡 Send /start to begin")
    
    # Keep alive
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped")
    except Exception as e:
        logger.exception(f"💥 Fatal error: {e}")
        raise