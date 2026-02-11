import logging
import os
import asyncio
import threading
import pysrt
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ChatAction
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
# 2. VOICE DATA & SAMPLES
# -------------------------------------------------------------------------
# The text the bot will speak when you preview a voice
SAMPLES = {
    "burmese": "မင်္ဂလာပါ၊ ဒါက ကျွန်တော့်ရဲ့ အသံပါ",  # "Hello, this is my voice"
    "japanese": "こんにちは、これは私の声です",       # "Hello, this is my voice"
    "english": "Hello, this is a sample of my voice quality.",
    "multilingual": "Hello, Mingalarbar. I can speak multiple languages."
}

VOICE_CATEGORIES = {
    "burmese": {
        "label": "🇲🇲 Burmese",
        "sample_text": SAMPLES["burmese"],
        "voices": {
            "Male (Thiha)": "my-MM-ThihaNeural",
            "Female (Nilar)": "my-MM-NilarNeural"
        }
    },
    "japanese": {
        "label": "🇯🇵 Japanese",
        "sample_text": SAMPLES["japanese"],
        "voices": {
            "Female (Nanami)": "ja-JP-NanamiNeural",
            "Male (Keita)": "ja-JP-KeitaNeural"
        }
    },
    "multilingual": {
        "label": "🌍 Multilingual",
        "sample_text": SAMPLES["multilingual"],
        "voices": {
            "Male (Andrew)": "en-US-AndrewMultilingualNeural",
            "Female (Ava)": "en-US-AvaMultilingualNeural"
        }
    },
    "english": {
        "label": "🇺🇸 English",
        "sample_text": SAMPLES["english"],
        "voices": {
            "Female (Aria)": "en-US-AriaNeural",
            "Male (Guy)": "en-US-GuyNeural"
        }
    }
}

# Flatten for easy lookup
ALL_VOICES = {}
for cat in VOICE_CATEGORIES.values():
    ALL_VOICES.update(cat['voices'])

# -------------------------------------------------------------------------
# 3. RENDER KEEPER
# -------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.wfile.write(b"Bot is alive!")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

# -------------------------------------------------------------------------
# 4. CORE FUNCTIONS
# -------------------------------------------------------------------------
async def srt_to_clean_text(text_content):
    """Clean SRT formatting if present."""
    if "-->" not in text_content:
        return text_content  # It's already plain text
    try:
        # Quick and dirty SRT cleanup without file I/O
        lines = text_content.split('\n')
        clean_lines = []
        for line in lines:
            # Skip timestamps and numbers
            if '-->' in line or line.strip().isdigit() or not line.strip():
                continue
            clean_lines.append(line.strip())
        return " ".join(clean_lines)
    except:
        return text_content

async def generate_audio(text, voice, filename):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filename)

# -------------------------------------------------------------------------
# 5. MESSAGE HANDLERS (The Fix for "No Response")
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Hello!**\n\nPaste any text or SRT content here.\n"
        "I will let you preview voices before generating.",
        parse_mode='Markdown'
    )

async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Collects text parts. If a message is long, Telegram splits it.
    We wait 2 seconds to see if more parts arrive.
    """
    user_id = update.effective_user.id
    text = update.message.text
    
    # 1. Initialize Buffer if empty
    if 'buffer' not in context.user_data:
        context.user_data['buffer'] = ""

    # 2. Add text to buffer
    context.user_data['buffer'] += text + "\n"
    
    # 3. Send "Typing" action so user knows we got it
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # 4. Cancel old timer, start new timer (Debouncing)
    if 'timer_job' in context.user_data:
        old_job = context.user_data['timer_job']
        old_job.schedule_removal()

    # Wait 2 seconds. If no new text comes, run 'process_buffer'
    context.user_data['timer_job'] = context.job_queue.run_once(
        process_buffer, 
        2.0, 
        chat_id=update.effective_chat.id, 
        user_id=user_id
    )

async def process_buffer(context: ContextTypes.DEFAULT_TYPE):
    """Called when the user stops sending text for 2 seconds."""
    job = context.job
    user_id = job.user_id
    full_text = context.user_data.get('buffer', "").strip()
    
    if not full_text: 
        return

    # Check length
    char_count = len(full_text)
    preview = full_text[:100] + "..." if char_count > 100 else full_text

    # Show Category Menu
    keyboard = []
    for key, data in VOICE_CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(data['label'], callback_data=f"cat_{key}")])
    
    keyboard.append([InlineKeyboardButton("❌ Clear", callback_data="clear")])

    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"📥 **Text Received** ({char_count} chars)\n\n_{preview}_\n\n👇 **Select a language to preview voices:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles SRT file uploads."""
    file = await update.message.document.get_file()
    byte_array = await file.download_as_bytearray()
    text = byte_array.decode('utf-8', errors='ignore')
    
    # Push to buffer and trigger processing immediately
    context.user_data['buffer'] = text
    context.job_queue.run_once(process_buffer, 0.1, chat_id=update.effective_chat.id, user_id=update.effective_user.id)

# -------------------------------------------------------------------------
# 6. CALLBACK HANDLER (The Preview Logic)
# -------------------------------------------------------------------------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    # --- CLEAR ---
    if data == "clear":
        context.user_data['buffer'] = ""
        await query.message.edit_text("🗑 Text cleared.")
        return

    # --- SHOW VOICES IN CATEGORY ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICE_CATEGORIES[cat_key]
        
        keyboard = []
        # Create buttons for each voice in this category
        for name, vid in category['voices'].items():
            # Pass category key too so we can find the sample text later
            keyboard.append([InlineKeyboardButton(f"▶️ Preview {name}", callback_data=f"prev_{cat_key}_{vid}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_home")])
        
        await query.message.edit_text(f"📂 **{category['label']}**\nSelect a voice to hear a sample:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # --- PLAY PREVIEW ---
    elif data.startswith("prev_"):
        _, cat_key, vid = data.split("_")
        
        # 1. Get sample text
        sample_text = VOICE_CATEGORIES[cat_key]['sample_text']
        
        # 2. Notify user
        await query.answer("🎧 Generating sample...")
        
        # 3. Generate Sample Audio
        sample_file = f"sample_{user_id}.mp3"
        await generate_audio(sample_text, vid, sample_file)
        
        # 4. Send Voice Note
        await context.bot.send_voice(chat_id=user_id, voice=open(sample_file, 'rb'), caption=f"🎙 Sample: {vid}")
        os.remove(sample_file)

        # 5. Show Confirm Button
        # We need to find the readable name for the button
        voice_name = next((k for k, v in VOICE_CATEGORIES[cat_key]['voices'].items() if v == vid), "Selected Voice")
        
        keyboard = [
            [InlineKeyboardButton(f"✅ Use {voice_name}", callback_data=f"gen_{vid}")],
            [InlineKeyboardButton("🔙 Pick another", callback_data=f"cat_{cat_key}")]
        ]
        await context.bot.send_message(chat_id=user_id, text=f"Did you like **{voice_name}**?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # --- GENERATE FULL AUDIO ---
    elif data.startswith("gen_"):
        vid = data.split("_")[1]
        buffer = context.user_data.get('buffer', "")
        
        if not buffer:
            await query.message.reply_text("❌ Text buffer empty. Please send text again.")
            return

        status_msg = await query.message.reply_text("⏳ **Generating full audio...** This may take a moment.", parse_mode="Markdown")
        
        try:
            # Clean SRT timestamps if needed
            clean_text = await srt_to_clean_text(buffer)
            
            output_file = f"full_{user_id}.mp3"
            await generate_audio(clean_text, vid, output_file)
            
            await context.bot.send_audio(
                chat_id=user_id, 
                audio=open(output_file, 'rb'), 
                title="Full Audio",
                caption="✅ Here is your audio."
            )
            
            os.remove(output_file)
            await status_msg.delete()
            context.user_data['buffer'] = "" # Clear buffer after success
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")

    # --- BACK ---
    elif data == "back_home":
        # Re-run the category menu logic
        keyboard = []
        for key, d in VOICE_CATEGORIES.items():
            keyboard.append([InlineKeyboardButton(d['label'], callback_data=f"cat_{key}")])
        keyboard.append([InlineKeyboardButton("❌ Clear", callback_data="clear")])
        await query.message.edit_text("👇 **Select a language:**", reply_markup=InlineKeyboardMarkup(keyboard))

# -------------------------------------------------------------------------
# 7. MAIN
# -------------------------------------------------------------------------
if __name__ == '__main__':
    if not TOKEN:
        print("❌ Error: TELEGRAM_TOKEN missing.")
        exit(1)
        
    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_text))
    app.add_handler(CallbackQueryHandler(button_click))

    print("🤖 Bot with Voice Previews is running...")
    app.run_polling()
