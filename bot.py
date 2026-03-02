import logging
import os
import re
import asyncio
import tempfile
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import edge_tts
from pydub import AudioSegment

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
if not TOKEN:
    print("❌ TELEGRAM_TOKEN missing.")
    exit(1)

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
# 3. HEALTH CHECK SERVER
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
# 4. SRT PARSER
# -------------------------------------------------------------------------
def timestamp_to_ms(ts):
    h, m, s_ms = ts.split(":")
    s, ms = s_ms.split(",")
    return (int(h)*3600 + int(m)*60 + int(s))*1000 + int(ms)

def parse_srt(srt_text):
    """
    Returns list of (start_ms, end_ms, clean_text)
    """
    pattern = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\s+([\s\S]*?)(?=\n\d+\n|\Z)", re.MULTILINE)
    entries = []
    for m in pattern.finditer(srt_text):
        start, end, text = m.groups()
        start_ms = timestamp_to_ms(start)
        end_ms = timestamp_to_ms(end)
        clean_text = " ".join(line.strip() for line in text.splitlines() if line.strip() and not line.strip().isdigit())
        entries.append((start_ms, end_ms, clean_text))
    return entries

# -------------------------------------------------------------------------
# 5. DUBBING FUNCTION
# -------------------------------------------------------------------------
async def generate_dub(entries, voice_id, output_file):
    final_audio = AudioSegment.silent(duration=0)
    tmp_files = []

    for start_ms, end_ms, text in entries:
        if not text.strip():
            continue
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
        communicate = edge_tts.Communicate(text, voice_id)
        await communicate.save(tmp_path)
        tmp_files.append(tmp_path)

        clip = AudioSegment.from_file(tmp_path)
        duration_ms = end_ms - start_ms
        # Adjust clip duration to match SRT duration
        if len(clip) < duration_ms:
            clip += AudioSegment.silent(duration=duration_ms - len(clip))
        elif len(clip) > duration_ms:
            clip = clip[:duration_ms]

        # Add gap if needed
        gap = start_ms - len(final_audio)
        if gap > 0:
            final_audio += AudioSegment.silent(duration=gap)

        final_audio += clip

    final_audio.export(output_file, format="mp3")

    # Cleanup temp files
    for f in tmp_files:
        os.remove(f)

# -------------------------------------------------------------------------
# 6. TELEGRAM HANDLERS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['buffer'] = ""
    await update.message.reply_text(
        "👋 **Long SRT Mode Ready**\n\n"
        "1. Send your SRT file or text parts.\n"
        "2. Keep sending parts if it's long.\n"
        "3. Click 'Done' when finished."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if 'buffer' not in context.user_data:
        context.user_data['buffer'] = ""

    new_text = ""
    if msg.document:
        status = await msg.reply_text("📂 **Reading file...**")
        file = await msg.document.get_file()
        byte_array = await file.download_as_bytearray()
        new_text = byte_array.decode('utf-8', errors='ignore')
        await status.delete()
    elif msg.text:
        new_text = msg.text

    context.user_data['buffer'] += "\n" + new_text
    current_len = len(context.user_data['buffer'])

    keyboard = [
        [InlineKeyboardButton("✅ Done / Dubbing", callback_data="menu_categories")],
        [InlineKeyboardButton("🗑 Clear All", callback_data="clear")]
    ]
    await msg.reply_text(
        f"📥 **Part Received**\nTotal Length: {current_len} chars\n\n👇 Send next part OR click Done:",
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
        keyboard = [[InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")] for key, info in VOICES.items()]
        await query.message.edit_text("🗣 **Select Language:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- SHOW VOICES ---
    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICES[cat_key]
        keyboard = [[InlineKeyboardButton(name, callback_data=f"tts_{vid}")] for name, vid in category['voices'].items()]
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

        await query.message.edit_text("⏳ **Generating Dubbing...**")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

        try:
            entries = parse_srt(full_text)
            if not entries:
                raise ValueError("No valid SRT entries found.")

            output_file = f"speech_{query.from_user.id}.mp3"
            await generate_dub(entries, voice_id, output_file)

            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=open(output_file, 'rb'),
                title="Dubbed Audio",
                caption=f"✅ Voice: {voice_id}"
            )

            os.remove(output_file)
            await query.message.delete()
            context.user_data['buffer'] = ""

        except Exception as e:
            await query.message.edit_text(f"❌ Error: {str(e)}")

# -------------------------------------------------------------------------
# 7. MAIN
# -------------------------------------------------------------------------
if __name__ == '__main__':
    Thread(target=start_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Dubbing Bot is running...")
    app.run_polling()
