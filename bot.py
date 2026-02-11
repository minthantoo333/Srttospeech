import logging
import os
import re
import asyncio
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatAction
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
# 4. SAFE TEXT CLEANER (REGEX)
# -------------------------------------------------------------------------
def clean_srt_text(text):
    """
    Safely removes timestamps (00:00:00 --> 00:00:05) and sequence numbers
    while preserving Japanese/Burmese characters.
    """
    try:
        lines = text.splitlines()
        clean_lines = []
        for line in lines:
            # 1. Remove Sequence Numbers (pure digits)
            if line.strip().isdigit():
                continue
            # 2. Remove Timestamps (contains -->)
            if '-->' in line:
                continue
            # 3. Keep text lines only
            if line.strip():
                clean_lines.append(line.strip())
        
        return " ".join(clean_lines)
    except Exception as e:
        logger.error(f"Clean Error: {e}")
        return text # Return original if regex fails

# -------------------------------------------------------------------------
# 5. HANDLERS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ **Bot is Online!**\nSend me your Japanese or Burmese SRT text.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Text and Files with Error Reporting"""
    try:
        msg = update.message
        
        # 1. Extract Text
        if msg.document:
            status = await msg.reply_text("📂 **Reading file...**")
            file = await msg.document.get_file()
            byte_array = await file.download_as_bytearray()
            text_content = byte_array.decode('utf-8', errors='ignore')
            await status.delete()
        elif msg.text:
            text_content = msg.text
        else:
            return # Ignore stickers/gifs

        # 2. Save Text to Memory
        context.user_data['current_text'] = text_content

        # 3. Detect SRT
        is_srt = "-->" in text_content
        preview = text_content[:80].replace("\n", " ") + "..."
        
        # 4. Create Keyboard
        keyboard = []
        for key, info in VOICES.items():
            keyboard.append([InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")])
        keyboard.append([InlineKeyboardButton("🗑 Clear", callback_data="clear")])

        header = "📜 **SRT Detected**" if is_srt else "📝 **Text Detected**"
        
        await msg.reply_text(
            f"{header}\n\n_{preview}_\n\n👇 **Select Language to Convert:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    except Exception as e:
        # 🚨 ERROR REPORTER: Tells you exactly what went wrong
        logger.error(f"Handler Error: {e}")
        await update.message.reply_text(f"❌ **Error:** {str(e)}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # --- CLEAR ---
    if data == "clear":
        context.user_data.pop('current_text', None)
        await query.message.edit_text("🗑 **Cleared.**")
        return

    # --- SHOW VOICES ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICES[cat_key]
        
        keyboard = []
        for name, vid in category['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"tts_{vid}")])
        
        # Back Button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])

        await query.message.edit_text(
            f"🗣 **Select {category['label']} Voice:**", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # --- BACK TO MAIN ---
    if data == "menu_main":
        keyboard = []
        for key, info in VOICES.items():
            keyboard.append([InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")])
        keyboard.append([InlineKeyboardButton("🗑 Clear", callback_data="clear")])
        await query.message.edit_text("👇 **Select Language:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- GENERATE AUDIO ---
    if data.startswith("tts_"):
        voice_id = data.split("_")[1]
        text = context.user_data.get('current_text')

        if not text:
            await query.message.edit_text("❌ Text expired. Send again.")
            return

        await query.message.edit_text("⏳ **Generating Audio...**")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

        try:
            # Clean Text
            final_text = clean_srt_text(text)

            # Generate
            output_file = f"speech_{query.from_user.id}.mp3"
            communicate = edge_tts.Communicate(final_text, voice_id)
            await communicate.save(output_file)

            # Send
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=open(output_file, 'rb'),
                title="Generated Speech",
                caption=f"✅ Voice: {voice_id}"
            )
            
            os.remove(output_file)
            await query.message.delete()

        except Exception as e:
            await query.message.edit_text(f"❌ Generation Failed: {str(e)}")

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
