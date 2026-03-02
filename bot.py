import logging
import os
import re
import asyncio
import edge_tts
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
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
# 4. SRT PARSER & SYNCED AUDIO GENERATOR
# -------------------------------------------------------------------------
def parse_time_to_ms(time_str):
    """Converts SRT timestamp '00:00:01,000' to milliseconds."""
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

def parse_srt(text):
    """Extracts start time, end time, and text from SRT format."""
    subs = []
    # Standardize line endings and split by empty lines
    blocks = text.strip().replace('\r\n', '\n').split('\n\n')
    for block in blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            time_line = lines[1]
            if '-->' in time_line:
                start_str, end_str = time_line.split(' --> ')
                start_ms = parse_time_to_ms(start_str.strip())
                end_ms = parse_time_to_ms(end_str.strip())
                text_content = " ".join(lines[2:]).strip()
                subs.append({'start': start_ms, 'end': end_ms, 'text': text_content})
    return subs

async def generate_synced_audio(srt_text, voice_id, final_output_file):
    subs = parse_srt(srt_text)
    if not subs:
        raise ValueError("Could not find valid SRT timestamps.")

    combined_audio = AudioSegment.silent(duration=0)
    current_time_ms = 0

    for sub in subs:
        start = sub['start']
        target_duration = sub['end'] - sub['start']
        text = sub['text']
        
        # Skip if there's no text to say
        if not text:
            continue

        # 1. Insert Silence to align with timestamp
        if current_time_ms < start:
            silence_duration = start - current_time_ms
            combined_audio += AudioSegment.silent(duration=silence_duration)
            current_time_ms = start

        # 2. Generate standard audio at +10%
        temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
        comm = edge_tts.Communicate(text, voice_id, rate="+10%")
        await comm.save(temp_file)

        # 3. Check Duration
        audio_seg = AudioSegment.from_mp3(temp_file)
        actual_duration = len(audio_seg)

        # 4. If too long, speed it up (Max 1.5x / +50%)
        if actual_duration > target_duration:
            ratio = actual_duration / target_duration
            required_speed = 1.1 * ratio  # Base speed was 1.1x
            
            # Cap the speed at 1.5x
            new_speed = min(1.5, required_speed)
            new_rate_percent = int((new_speed - 1.0) * 100)
            
            os.remove(temp_file)
            # Re-generate with new speed
            comm = edge_tts.Communicate(text, voice_id, rate=f"+{new_rate_percent}%")
            await comm.save(temp_file)
            audio_seg = AudioSegment.from_mp3(temp_file)

        # 5. Append to main timeline
        combined_audio += audio_seg
        current_time_ms += len(audio_seg)
        os.remove(temp_file)

    # Export final stitched audio (running in background to not block async loop)
    await asyncio.to_thread(combined_audio.export, final_output_file, format="mp3")


# -------------------------------------------------------------------------
# 5. HANDLERS
# -------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['buffer'] = ""
    await update.message.reply_text(
        "👋 **Synced SRT Mode Ready**\n\n"
        "1. Send your first SRT file/text.\n"
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

    context.user_data['buffer'] += "\n\n" + new_text
    
    keyboard = [
        [InlineKeyboardButton("✅ Done / Convert", callback_data="menu_categories")],
        [InlineKeyboardButton("🗑 Clear All", callback_data="clear")]
    ]
    
    await msg.reply_text(
        f"📥 **Part Received.**\n\n👇 **Send next part OR Click Done:**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "clear":
        context.user_data['buffer'] = ""
        await query.message.edit_text("🗑 **Buffer Cleared.** Start over.")
        return

    if data == "menu_categories":
        if not context.user_data.get('buffer'):
            await query.message.edit_text("❌ Buffer empty.")
            return

        keyboard = [[InlineKeyboardButton(info['label'], callback_data=f"cat_{key}")] for key, info in VOICES.items()]
        await query.message.edit_text("🗣 **Select Language:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("cat_"):
        cat_key = data.split("_")[1]
        category = VOICES[cat_key]
        keyboard = [[InlineKeyboardButton(name, callback_data=f"tts_{vid}")] for name, vid in category['voices'].items()]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_categories")])
        await query.message.edit_text(f"🗣 **Select {category['label']} Voice:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("tts_"):
        voice_id = data.split("_")[1]
        full_text = context.user_data.get('buffer', "")

        if not full_text:
            await query.message.edit_text("❌ Text expired.")
            return

        await query.message.edit_text("⏳ **Parsing Timestamps & Generating Audio...**\n_(This will take longer because of timeline stitching)_")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)

        output_file = f"synced_speech_{query.from_user.id}.mp3"
        
        try:
            # Process the synced audio
            await generate_synced_audio(full_text, voice_id, output_file)

            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=open(output_file, 'rb'),
                title="Synced Audio",
                caption=f"✅ Voice: {voice_id}\n⚡️ Synced & Speed-Adjusted"
            )
            
            os.remove(output_file)
            context.user_data['buffer'] = "" 

        except Exception as e:
            await query.message.edit_text(f"❌ Error: {str(e)}\n\n(Did you install FFmpeg?)")

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

    print("🤖 Synced Bot is running...")
    app.run_polling()
