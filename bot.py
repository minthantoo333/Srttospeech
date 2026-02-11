import logging
import os
import re
import asyncio
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters

# -------------------------------------------------------------------------
# 1. LOGGING & CONFIG
# -------------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# -------------------------------------------------------------------------
# 2. VOICE LIST
# -------------------------------------------------------------------------
VOICES = {
    "japanese": {
        "label": "🇯🇵 Japanese",
        "voices": {
            "Female (Nanami)": "ja-JP-NanamiNeural",
            "Male (Keita)": "ja-JP-KeitaNeural"
        }
    },
    "burmese": {
        "label": "🇲🇲 Burmese",
        "voices": {
            "Male (Thiha)": "my-MM-ThihaNeural",
            "Female (Nilar)": "my-MM-NilarNeural"
        }
    },
    "english": {
        "label": "🇺🇸 English",
        "voices": {
            "Female (Aria)": "en-US-AriaNeural",
            "Male (Guy)": "en-US-GuyNeural"
        }
    },
    "multilingual": {
        "label": "🌍 Multilingual",
        "voices": {
            "Male (Andrew)": "en-US-AndrewMultilingualNeural",
            "Female (Ava)": "en-US-AvaMultilingualNeural"
        }
    }
}

# -------------------------------------------------------------------------
# 3. RENDER KEEPER
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.wfile.write(b"Alive")

def start_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# -------------------------------------------------------------------------
# 4. SAFE TEXT CLEANER
# -------------------------------------------------------------------------
def clean_srt_text(text):
    """Safely removes timestamps and numbers."""
    try:
        lines = text.splitlines()
        clean_lines = []
        for line in lines:
            if line.strip().isdigit(): continue
            if '-->' in line: continue
            if line.strip(): clean_lines.append(line.strip())
        return " ".join(clean_lines)
    except:
        return text

# -------------------------------------------------------------------------
# 5. HANDLERS (The Append Logic)
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reset buffer on start
    context.user_data['buffer'] = ""
    await update.message.reply_text(
        "👋 **Long SRT Mode Ready**\n\n"
        "1. Send your first message.\n"
        "2. Keep sending parts if it's long.\n"
        "3. Click 'Done' when finished."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id
    
    # Initialize buffer if empty
    if 'buffer' not in context.user_data:
        context.user_data['buffer'] = ""

    # Get New Content
    new_text = ""
    if msg.document:
        status = await msg.reply_text("📂 **Reading file...**")
        file = await msg.document.get_file()
        byte_array = await file.download_as_bytearray()
        new_text = byte_array.decode('utf-8', errors='ignore')
        await status.delete()
    elif msg.text:
        new_text = msg.text

    # Append to Buffer
    # Add a newline to ensure smooth joining if Telegram split mid-line
    context.user_data['buffer'] += "\n" + new_text
    
    current_len = len(context.user_data['buffer'])
    
    # Send "Part Received" Menu
    keyboard = [
        [InlineKeyboardButton("✅ Done / Convert", callback_data="menu_categories")],
        [InlineKeyboardButton("🗑 Clear All", callback_data="clear")]
    ]
    
    await msg.reply_text(
        f"📥 **Part Received**\nTotal Length: {current_len} chars\n\n👇 **Send next part OR Click Done:**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # --- CLEAR ---
    if data == "clear":
        context.user_data['buffer'] = ""
        await query.message.edit_text("🗑 **Buffer Cleared.** Start over.")
        return

    # --- SHOW CATEGORIES ---
    if data == "menu_categories":
        if not context.user_data.get('buffer'):
            await query.message.edit_text("❌ Buffer empty.")
            return

        keyboard = []
        for key, info in VOICES.items():
            keyboard.append([InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")])
        
        await query.message.edit_text("🗣 **Select Language:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- SHOW VOICES ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICES[cat_key]
        keyboard = []
        for name, vid in category['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"tts_{vid}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_categories")])
        await query.message.edit_text(f"🗣 **Select {category['label']} Voice:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- GENERATE AUDIO ---
    if data.startswith("tts_"):
        voice_id = data.split("_")[1]
        full_text = context.user_data.get('buffer', "")

        if not full_text:
            await query.message.edit_text("❌ Text expired.")
            return

        await query.message.edit_text("⏳ **Merging & Generating...**")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

        try:
            final_text = clean_srt_text(full_text)
            
            # Use temp filename based on user ID
            output_file = f"speech_{query.from_user.id}.mp3"
            communicate = edge_tts.Communicate(final_text, voice_id)
            await communicate.save(output_file)

            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=open(output_file, 'rb'),
                title="Full Audio",
                caption=f"✅ Voice: {voice_id}"
            )
            
            os.remove(output_file)
            await query.message.delete()
            # Clear buffer after success
            context.user_data['buffer'] = "" 

        except Exception as e:
            await query.message.edit_text(f"❌ Error: {str(e)}")

# -------------------------------------------------------------------------
# 6. MAIN
# -------------------------------------------------------------------------
if __name__ == '__main__':
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN missing.")
        exit(1)
        
    Thread(target=start_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Bot is running...")
    app.run_polling()
