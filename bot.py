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
# 1. CONFIGURATION & LOGGING
# -------------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Default settings if user hasn't chosen one
DEFAULT_VOICE = "en-US-AriaNeural" 

# -------------------------------------------------------------------------
# 2. VOICE DATABASE (Categorized)
# -------------------------------------------------------------------------
VOICE_CATEGORIES = {
    "burmese": {
        "label": "🇲🇲 Burmese",
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
            "Male (Andrew)": "en-US-AndrewMultilingualNeural",
            "Female (Ava)": "en-US-AvaMultilingualNeural",
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
    }
}

# Flatten voices for easy lookup later
ALL_VOICES_FLAT = {}
for cat in VOICE_CATEGORIES.values():
    ALL_VOICES_FLAT.update(cat['voices'])

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
    server.serve_forever()

# -------------------------------------------------------------------------
# 4. UTILITY FUNCTIONS
# -------------------------------------------------------------------------
def get_user_voice(context, user_id):
    """Retrieve user's preferred voice or default."""
    return context.user_data.get('voice', DEFAULT_VOICE)

async def srt_to_clean_text(srt_content):
    """Parses SRT string and returns clean text."""
    try:
        # We save to a temp file because pysrt expects a file or clean string
        with open("temp.srt", "w", encoding="utf-8") as f:
            f.write(srt_content)
        subs = pysrt.open("temp.srt")
        text = " ".join([sub.text.replace('\n', ' ') for sub in subs])
        return text
    except Exception:
        # Fallback: simple text cleanup if pysrt fails
        return srt_content.replace("\n", " ")

async def generate_audio(text, voice_id, output_file):
    communicate = edge_tts.Communicate(text, voice_id)
    await communicate.save(output_file)

# -------------------------------------------------------------------------
# 5. HANDLERS: COMMANDS & SETTINGS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **SRT to Speech Bot**\n\n"
        "**How to use:**\n"
        "1. Upload an `.srt` file **OR**\n"
        "2. Paste SRT text directly here.\n\n"
        "⚙️ **Commands:**\n"
        "/settings - Choose your default voice.\n"
        "/clear - Clear text buffer.",
        parse_mode='Markdown'
    )

async def clear_buffer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['buffer'] = ""
    await update.message.reply_text("🧹 Text buffer cleared.")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show Category Menu
    keyboard = []
    for key, data in VOICE_CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(data['label'], callback_data=f"setcat_{key}")])
    
    current_voice = get_user_voice(context, update.effective_user.id)
    
    # Find readable name
    voice_name = "Default"
    for name, vid in ALL_VOICES_FLAT.items():
        if vid == current_voice:
            voice_name = name
            break

    text = f"⚙️ **Settings**\n\n**Current Voice:** `{voice_name}`\n\nSelect a category to change:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# -------------------------------------------------------------------------
# 6. HANDLERS: TEXT & FILE PROCESSING (SMART BUFFER)
# -------------------------------------------------------------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # 1. Initialize Buffer
    if 'buffer' not in context.user_data:
        context.user_data['buffer'] = ""

    # 2. Append Text
    context.user_data['buffer'] += text + "\n"

    # 3. Debounce (Auto-Combine Logic)
    # Cancel previous timer if exists
    if 'job' in context.user_data:
        job = context.user_data['job']
        job.schedule_removal()

    # Schedule new processing in 2 seconds (Wait for next split part)
    context.user_data['job'] = context.job_queue.run_once(
        process_buffered_text, 
        2.0, 
        chat_id=update.effective_chat.id, 
        user_id=user_id,
        data=user_id
    )

async def process_buffered_text(context: ContextTypes.DEFAULT_TYPE):
    """Called after 2 seconds of silence."""
    job = context.job
    user_id = job.user_id
    full_text = context.user_data.get('buffer', "")

    if not full_text.strip():
        return

    # Check if it looks like SRT (basic check)
    is_srt = "-->" in full_text and "\n" in full_text
    
    msg_type = "SRT" if is_srt else "Text"
    char_count = len(full_text)
    
    # Get user's preferred voice
    current_voice_id = get_user_voice(context, user_id)
    
    # Find voice name for display
    voice_label = "Unknown"
    for name, vid in ALL_VOICES_FLAT.items():
        if vid == current_voice_id:
            voice_label = name
            break

    # Keyboard to Confirm
    keyboard = [
        [InlineKeyboardButton(f"🎙 Generate ({voice_label})", callback_data="gen_default")],
        [InlineKeyboardButton("🎨 Change Voice & Generate", callback_data="gen_choose")],
        [InlineKeyboardButton("❌ Cancel / Clear", callback_data="clear_data")]
    ]
    
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"📥 **Received {msg_type}**\nLength: {char_count} chars\n\nI have combined your messages. Ready to speak?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.file_name.lower().endswith('.srt'):
        await update.message.reply_text("❌ Please send a valid .srt file.")
        return
    
    file = await context.bot.get_file(document.file_id)
    # Download content directly to memory string
    byte_array = await file.download_as_bytearray()
    text_content = byte_array.decode('utf-8', errors='ignore')
    
    # Put into buffer and trigger processing immediately
    context.user_data['buffer'] = text_content
    
    # Manually trigger the "Received" prompt
    context.job_queue.run_once(
        process_buffered_text, 
        0.1, 
        chat_id=update.effective_chat.id, 
        user_id=update.effective_user.id
    )

# -------------------------------------------------------------------------
# 7. CALLBACK HANDLER (Buttons)
# -------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # --- ACTION: CLEAR ---
    if data == "clear_data":
        context.user_data['buffer'] = ""
        await query.edit_message_text("🗑 Buffer cleared.")
        return

    # --- ACTION: GENERATE (DEFAULT) ---
    if data == "gen_default":
        await run_generation(update, context, get_user_voice(context, user_id))
        return

    # --- ACTION: CHOOSE VOICE MENU ---
    if data == "gen_choose":
        # Show Categories
        keyboard = []
        for key, cat_data in VOICE_CATEGORIES.items():
            keyboard.append([InlineKeyboardButton(cat_data['label'], callback_data=f"pickcat_{key}")])
        await query.edit_message_text("📂 Select Category:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- SETTINGS: SET CATEGORY ---
    if data.startswith("setcat_"):
        cat = data.split("_")[1]
        keyboard = []
        for name, vid in VOICE_CATEGORIES[cat]['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"setvoice_{vid}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_settings")])
        await query.edit_message_text(f"Select default voice for {cat}:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- SETTINGS: SAVE VOICE ---
    if data.startswith("setvoice_"):
        vid = data.split("_")[1]
        context.user_data['voice'] = vid
        await query.edit_message_text(f"✅ Default voice updated!")
        return
    
    # --- SETTINGS: BACK ---
    if data == "back_settings":
        await settings_command(update, context) # Re-run settings menu
        return

    # --- TEMP CHOICE: CATEGORY ---
    if data.startswith("pickcat_"):
        cat = data.split("_")[1]
        keyboard = []
        for name, vid in VOICE_CATEGORIES[cat]['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"run_{vid}")])
        await query.edit_message_text(f"Select voice:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- TEMP CHOICE: RUN ---
    if data.startswith("run_"):
        vid = data.split("_")[1]
        await run_generation(update, context, vid)
        return

async def run_generation(update, context, voice_id):
    query = update.callback_query
    buffer = context.user_data.get('buffer', "")
    
    if not buffer:
        await query.edit_message_text("❌ Text buffer is empty.")
        return

    await query.edit_message_text(f"⏳ Generating audio...")

    try:
        # 1. Clean Text (if SRT)
        clean_text = await srt_to_clean_text(buffer)
        
        # 2. Generate
        os.makedirs("downloads", exist_ok=True)
        output_file = f"downloads/{query.from_user.id}.mp3"
        await generate_audio(clean_text, voice_id, output_file)
        
        # 3. Send
        await context.bot.send_audio(
            chat_id=update.effective_chat.id,
            audio=open(output_file, 'rb'),
            title="Speech Audio",
            caption="✅ Generated successfully."
        )
        
        # Cleanup
        os.remove(output_file)
        context.user_data['buffer'] = "" # Auto-clear after success
        
    except Exception as e:
        logger.error(f"Gen Error: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Error generating audio.")

# -------------------------------------------------------------------------
# 8. MAIN
# -------------------------------------------------------------------------
if __name__ == '__main__':
    if not TOKEN:
        print("❌ Error: TELEGRAM_TOKEN missing.")
        exit(1)
        
    threading.Thread(target=start_health_server, daemon=True).start()

    # Note: JobQueue is enabled by default in ApplicationBuilder
    application = ApplicationBuilder().token(TOKEN).build()

    # Commands
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('settings', settings_command))
    application.add_handler(CommandHandler('clear', clear_buffer))

    # Messages
    application.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Callback (Buttons)
    application.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Bot with Smart Buffer is running...")
    application.run_polling()
