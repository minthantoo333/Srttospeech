import logging
import os
import asyncio
import threading
import pysrt
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters

# -------------------------------------------------------------------------
# 1. CONFIGURATION
# -------------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# -------------------------------------------------------------------------
# 2. VOICE DATABASE
# -------------------------------------------------------------------------
# We organize voices by category to make the menu cleaner.
VOICE_CATEGORIES = {
    "burmese": {
        "label": "🇲🇲 Burmese (Myanmar)",
        "voices": {
            "Male (Thiha)": "my-MM-ThihaNeural",
            "Female (Nilar)": "my-MM-NilarNeural"
        }
    },
    "japanese": {
        "label": "🇯🇵 Japanese",
        "voices": {
            "Female (Nanami)": "ja-JP-NanamiNeural",
            "Male (Keita)": "ja-JP-KeitaNeural"
        }
    },
    "multilingual": {
        "label": "🌍 Multilingual (Mixed)",
        "voices": {
            "Male (Andrew - Best)": "en-US-AndrewMultilingualNeural",
            "Female (Ava - Best)": "en-US-AvaMultilingualNeural",
            "Male (Brian)": "en-US-BrianMultilingualNeural",
            "Female (Emma)": "en-US-EmmaMultilingualNeural"
        }
    },
    "english": {
        "label": "🇺🇸/🇬🇧 English",
        "voices": {
            "🇺🇸 Female (Aria)": "en-US-AriaNeural",
            "🇺🇸 Male (Guy)": "en-US-GuyNeural",
            "🇬🇧 Female (Sonia)": "en-GB-SoniaNeural",
            "🇬🇧 Male (Ryan)": "en-GB-RyanNeural"
        }
    },
    "others": {
        "label": "🌏 Other Asian",
        "voices": {
            "🇨🇳 Chinese (Xiaoxiao)": "zh-CN-XiaoxiaoNeural",
            "🇨🇳 Chinese (Yunxi)": "zh-CN-YunxiNeural",
            "🇰🇷 Korean (SunHi)": "ko-KR-SunHiNeural",
            "🇹🇭 Thai (Premwadee)": "th-TH-PremwadeeNeural"
        }
    }
}

# -------------------------------------------------------------------------
# 3. RENDER KEEPER (Web Server)
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌍 Health server started on port {port}")
    server.serve_forever()

# -------------------------------------------------------------------------
# 4. UTILITY FUNCTIONS
# -------------------------------------------------------------------------
async def srt_to_clean_text(srt_path):
    """Parses SRT and returns clean text without timestamps."""
    try:
        subs = pysrt.open(srt_path)
        # Combine text, removing newlines to create a smooth flow
        text = " ".join([sub.text.replace('\n', ' ') for sub in subs])
        return text
    except Exception as e:
        logger.error(f"SRT Parse Error: {e}")
        return None

async def generate_audio(text, voice_id, output_file):
    """Generates MP3 using Edge TTS."""
    communicate = edge_tts.Communicate(text, voice_id)
    await communicate.save(output_file)

# -------------------------------------------------------------------------
# 5. BOT HANDLERS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **SRT to Speech Bot**\n\n"
        "1. Send me an `.srt` file.\n"
        "2. Choose a language.\n"
        "3. Get the MP3 audio.\n\n"
        "🚀 Supports **Burmese, Japanese, Multilingual**, and more.",
        parse_mode='Markdown'
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    file_name = document.file_name

    if not file_name.lower().endswith('.srt'):
        await update.message.reply_text("❌ Please send a valid **.srt** file.")
        return

    # Download file
    file = await context.bot.get_file(document.file_id)
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{document.file_unique_id}.srt"
    await file.download_to_drive(file_path)

    # Save path to user context
    context.user_data['srt_path'] = file_path

    # Step 1: Show Categories
    keyboard = []
    for key, data in VOICE_CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(data['label'], callback_data=f"cat_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📂 **Select Language Category:**", reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- LEVEL 1: Handle Category Selection ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICE_CATEGORIES.get(cat_key)
        
        if not category:
            await query.edit_message_text("Error: Category not found.")
            return

        # Build keyboard for specific voices in this category
        keyboard = []
        for name, voice_id in category['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"voice_{voice_id}")])
        
        # Add a "Back" button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"🗣 **Select {category['label']} Voice:**", reply_markup=reply_markup)
        return

    # --- LEVEL 2: Handle "Back" Button ---
    if data == "back_main":
        keyboard = []
        for key, val in VOICE_CATEGORIES.items():
            keyboard.append([InlineKeyboardButton(val['label'], callback_data=f"cat_{key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📂 **Select Language Category:**", reply_markup=reply_markup)
        return

    # --- LEVEL 3: Handle Voice Selection & Generation ---
    if data.startswith("voice_"):
        voice_id = data.split("_")[1]
        srt_path = context.user_data.get('srt_path')

        if not srt_path or not os.path.exists(srt_path):
            await query.edit_message_text("❌ File expired. Please upload again.")
            return

        await query.edit_message_text(f"⏳ **Processing...**\nVoice ID: `{voice_id}`", parse_mode="Markdown")

        try:
            # 1. Parse SRT
            text_content = await srt_to_clean_text(srt_path)
            
            if not text_content or not text_content.strip():
                await query.edit_message_text("❌ SRT file is empty or unreadable.")
                return

            # 2. Generate Audio
            output_audio = srt_path.replace(".srt", ".mp3")
            await generate_audio(text_content, voice_id, output_audio)

            # 3. Send Audio
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=open(output_audio, 'rb'),
                title=f"Audio_{voice_id}",
                caption="✅ Here is your speech file!"
            )

            # Cleanup
            if os.path.exists(srt_path): os.remove(srt_path)
            if os.path.exists(output_audio): os.remove(output_audio)

        except Exception as e:
            logger.error(f"Generation Error: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="❌ Failed to generate audio. The text might be too long."
            )

# -------------------------------------------------------------------------
# 6. MAIN EXECUTION
# -------------------------------------------------------------------------
if __name__ == '__main__':
    if not TOKEN:
        print("❌ Error: TELEGRAM_TOKEN is missing.")
        exit(1)
        
    # Start Health Server (Daemon Thread)
    threading.Thread(target=start_health_server, daemon=True).start()

    # Build Bot
    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_document))
    application.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Bot is running...")
    application.run_polling()
