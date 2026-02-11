import logging
import os
import asyncio
import pysrt
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatAction
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
VOICES = {
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
# 3. RENDER KEEPER (Prevents sleeping)
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
# 4. UTILITY FUNCTIONS
# -------------------------------------------------------------------------
async def clean_srt(text):
    """Removes timestamps from SRT text."""
    try:
        # Simple parsing: split by double newline
        blocks = text.strip().split('\n\n')
        clean_lines = []
        for block in blocks:
            lines = block.split('\n')
            # Usually line 0 is index, line 1 is time, line 2+ is text
            if len(lines) >= 3 and '-->' in lines[1]:
                clean_lines.append(" ".join(lines[2:]))
            else:
                # Fallback for non-standard SRT
                clean_lines.append(block)
        return " ".join(clean_lines)
    except:
        return text

# -------------------------------------------------------------------------
# 5. MESSAGE HANDLERS (Instant Response)
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Ready!**\nPaste your SRT text or upload a file.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles both Text messages and File uploads instantly.
    """
    msg = update.message
    
    # 1. Get the Content
    if msg.document:
        status = await msg.reply_text("📂 **Reading file...**")
        file = await msg.document.get_file()
        byte_array = await file.download_as_bytearray()
        text_content = byte_array.decode('utf-8', errors='ignore')
        await status.delete()
    else:
        text_content = msg.text

    # 2. Save to User Memory
    context.user_data['current_text'] = text_content

    # 3. Detect SRT
    is_srt = "-->" in text_content and "\n" in text_content
    preview = text_content[:100].replace("\n", " ") + "..."
    
    header = "📜 **SRT Detected**" if is_srt else "📝 **Text Detected**"
    
    # 4. Create "Generate" Button
    keyboard = [
        [InlineKeyboardButton("🎙 Generate Speech Now", callback_data="menu_categories")],
        [InlineKeyboardButton("🗑 Clear", callback_data="clear")]
    ]
    
    await msg.reply_text(
        f"{header}\n\n_{preview}_\n\nTap below to convert to audio:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# -------------------------------------------------------------------------
# 6. BUTTON HANDLER
# -------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # --- CLEAR ---
    if data == "clear":
        context.user_data.pop('current_text', None)
        await query.message.edit_text("🗑 **Cleared.** Send new text.")
        return

    # --- SHOW CATEGORIES (Language List) ---
    if data == "menu_categories":
        keyboard = []
        for key, info in VOICES.items():
            keyboard.append([InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")])
        
        await query.message.edit_text(
            "🗣 **Select Language:**", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # --- SHOW VOICES (Male/Female) ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICES[cat_key]
        
        keyboard = []
        for name, vid in category['voices'].items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"tts_{vid}")])
        
        # Back Button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_categories")])

        await query.message.edit_text(
            f"🗣 **Select {category['label']} Voice:**", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # --- GENERATE AUDIO ---
    if data.startswith("tts_"):
        voice_id = data.split("_")[1]
        text = context.user_data.get('current_text')

        if not text:
            await query.message.edit_text("❌ Text expired. Please send it again.")
            return

        await query.message.edit_text("⏳ **Generating Audio...**\n_(This may take a few seconds)_", parse_mode="Markdown")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

        try:
            # Clean SRT timestamps before speaking
            if "-->" in text:
                final_text = await clean_srt(text)
            else:
                final_text = text

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
            
            # Cleanup
            os.remove(output_file)
            await query.message.delete() # Remove the "Generating..." message

        except Exception as e:
            await query.message.edit_text(f"❌ Error: {str(e)}")

# -------------------------------------------------------------------------
# 7. MAIN EXECUTION
# -------------------------------------------------------------------------
if __name__ == '__main__':
    if not TOKEN:
        print("❌ Error: TELEGRAM_TOKEN missing.")
        exit(1)
        
    # Start Health Server in Background
    Thread(target=start_server, daemon=True).start()

    # Bot Setup
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Bot is running...")
    app.run_polling()
