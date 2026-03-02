import os
import glob
import asyncio
import pysrt
import re 
from aiohttp import web # Added for Uptime web server

# Audio Processing
from pydub import AudioSegment, effects
from pydub.silence import detect_leading_silence
import edge_tts

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters

# --- ⚙️ CONFIGURATION ---
TG_TOKEN = os.getenv("TG_TOKEN")
PORT = int(os.environ.get("PORT", 8080)) # Port for Render/UptimeRobot

if not TG_TOKEN:
    print("❌ ERROR: TG_TOKEN is missing! Set it in your environment variables.")
    exit()

# --- 🗣️ VOICE LIBRARY ---
VOICE_LIB = {
    "🇯🇵 Japanese (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Japanese (Male)": "ja-JP-KeitaNeural",
    "🇲🇲 Burmese (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Burmese (Female)": "my-MM-NularNeural",
    "🇺🇸 Remy (Multi)": "en-US-RemyMultilingualNeural",
    "🇮🇹 Giuseppe (Multi)": "it-IT-GiuseppeMultilingualNeural",
    "🇺🇸 Brian (Male)": "en-US-BrianNeural",
    "🇺🇸 Andrew (Male)": "en-US-AndrewNeural"
}

# Folders
BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

# User Data
user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "ja-JP-NanamiNeural", # Changed default to JP for testing 
        }
    return user_prefs[user_id]

def get_paths(user_id):
    return {
        "srt": f"downloads/{user_id}_subs.srt",
        "dub_audio": f"downloads/{user_id}_dubbed.mp3"
    }

def clean_temp(user_id):
    for f in glob.glob(f"temp/{user_id}_chunk_*.mp3"):
        try: os.remove(f)
        except: pass

def wipe_user_data(user_id):
    for f in glob.glob(f"downloads/{user_id}_*"):
        try: os.remove(f)
        except: pass
    clean_temp(user_id)
    if user_id in user_prefs: del user_prefs[user_id]

# --- 🔊 AUDIO POST-PROCESSING ---
def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100:  
        return audio_segment
        
    start_trim = detect_leading_silence(audio_segment, silence_threshold=silence_thresh, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio_segment.reverse(), silence_threshold=silence_thresh, chunk_size=chunk_size)
    
    duration = len(audio_segment)
    trimmed = audio_segment[start_trim:duration-end_trim]
    return trimmed

def make_audio_crisp(audio_segment):
    clean = audio_segment.high_pass_filter(150)
    return effects.normalize(clean)

# --- 🎬 DUBBING ENGINE ---
async def generate_dubbing(user_id, srt_path, output_path, voice):
    print(f"🎬 Starting Dubbing for {user_id}...")
    try:
        subs = pysrt.open(srt_path)
        final_audio = AudioSegment.empty()
        current_timeline_ms = 0
        
        BASE_RATE_VAL = 10 
        PITCH_VAL = "-2Hz"

        for i, sub in enumerate(subs):
            start_ms = (sub.start.hours * 3600 + sub.start.minutes * 60 + sub.start.seconds) * 1000 + sub.start.milliseconds
            end_ms = (sub.end.hours * 3600 + sub.end.minutes * 60 + sub.end.seconds) * 1000 + sub.end.milliseconds
            allowed_duration_ms = end_ms - start_ms
            
            text = sub.text.replace("\n", " ").strip()
            if not text: continue 

            if start_ms > current_timeline_ms:
                gap = start_ms - current_timeline_ms
                if gap > 100:
                    final_audio += AudioSegment.silent(duration=gap)
                    current_timeline_ms += gap

            temp_filename = f"temp/{user_id}_chunk_{i}.mp3"
            
            communicate = edge_tts.Communicate(text, voice, rate=f"+{BASE_RATE_VAL}%", pitch=PITCH_VAL)
            await communicate.save(temp_filename)
            
            segment = AudioSegment.from_file(temp_filename)
            segment = trim_silence(segment)

            current_len = len(segment)
            if current_len > allowed_duration_ms:
                ratio = current_len / allowed_duration_ms
                extra_speed_needed = (ratio - 1) * 100
                new_rate = int(BASE_RATE_VAL + extra_speed_needed + 5)
                if new_rate > 50: new_rate = 50
                
                communicate = edge_tts.Communicate(text, voice, rate=f"+{new_rate}%", pitch=PITCH_VAL)
                await communicate.save(temp_filename)
                
                segment = AudioSegment.from_file(temp_filename)
                segment = trim_silence(segment)

            segment = make_audio_crisp(segment)
            final_audio += segment
            current_timeline_ms += len(segment)
            
            if os.path.exists(temp_filename): os.remove(temp_filename)

        final_audio.export(output_path, format="mp3")
        clean_temp(user_id)
        return True, None

    except Exception as e:
        return False, str(e)

# --- 🤖 HANDLERS ---
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("voices", "🗣️ Change Voice"),
        BotCommand("dub", "🎬 Dub Audio"),
        BotCommand("clearall", "🧹 Clear Data")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    keyboard = [
        [InlineKeyboardButton(f"🗣️ Voice: {voice_name}", callback_data="cmd_voices")]
    ]
    await update.message.reply_text("👋 **SRT Dubbing Studio**\nSend me an `.srt` file or paste SRT text to get started.", reply_markup=InlineKeyboardMarkup(keyboard))

async def voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for name, code in VOICE_LIB.items():
        row.append(InlineKeyboardButton(name, callback_data=f"set_voice_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text("🗣️ **Select Narrator Voice:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wipe_user_data(update.effective_user.id)
    await update.message.reply_text("🧹 **All temporary files cleared.**")

async def dub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await perform_dubbing(update, context)

async def perform_dubbing(update, context):
    user_id = update.effective_user.id
    p = get_paths(user_id)
    state = get_user_state(user_id)
    msg = update.effective_message

    if not os.path.exists(p['srt']):
        await msg.reply_text("❌ **No SRT found. Please send an .srt file first.**")
        return

    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Selected Voice")
    status = await msg.reply_text(f"🎬 **Dubbing ({voice_name})... This might take a minute.**")
    
    success, error = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state['dub_voice'])
    
    if success:
        await status.delete()
        await context.bot.send_audio(chat_id=msg.chat_id, audio=open(p['dub_audio'], "rb"), caption=f"✅ **Dubbed successfully by {voice_name}!**")
    else:
        await status.edit_text(f"❌ Dubbing Failed: {error}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data
    
    if data == "cmd_voices":
        await voices_command(update, context)
        await query.answer()

    elif data.startswith("set_voice_"):
        new_voice = data.replace("set_voice_", "")
        state['dub_voice'] = new_voice
        v_name = next((k for k, v in VOICE_LIB.items() if v == new_voice), "Custom Voice")
        await query.message.edit_text(f"✅ Voice set to: **{v_name}**")

    elif data == "trigger_dub":
        await perform_dubbing(update, context)
        await query.answer()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    text = msg.text
    p = get_paths(user_id)

    if re.search(r'\d{2}:\d{2}:\d{2},\d{3} -->', text):
        file_mode = 'a'
        if re.match(r'^\s*1\s*$', text.split('\n')[0].strip()) or text.strip().startswith('1\n'):
            file_mode = 'w'
        
        with open(p['srt'], file_mode, encoding="utf-8") as f: f.write(text + "\n")
        
        keyboard = [[InlineKeyboardButton("🎬 Dub Audio", callback_data="trigger_dub")]]
        await msg.reply_text("✅ **SRT Text Detected and Saved!**\nWant to generate audio now?", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.reply_text("ℹ️ Please send an `.srt` file or paste valid SRT text format.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)
    file_obj = await msg.document.get_file()
    name = msg.document.file_name
    
    if name.lower().endswith('.srt'):
        await file_obj.download_to_drive(p['srt'])
        keyboard = [[InlineKeyboardButton("🎬 Dub Audio", callback_data="trigger_dub")]]
        await msg.reply_text("✅ **SRT File Loaded.** Ready to dub!", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.reply_text("❌ Please send an `.srt` subtitle file.")


# --- 🌐 UPTIME WEB SERVER ---
async def health_check(request):
    """Responds to GET and HEAD requests to keep Render/UptimeRobot happy"""
    return web.Response(text="Bot is running!", status=200)

async def run_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_head('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"🌐 Uptime Web Server running on port {PORT}")


# --- 🚀 MAIN LOOP ---
async def main():
    print("🚀 Initializing SRT Dubbing Bot...")
    
    # Initialize Telegram Bot
    bot_app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("voices", voices_command))
    bot_app.add_handler(CommandHandler("dub", dub_command))
    bot_app.add_handler(CommandHandler("clearall", clearall_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    # Start bot and server simultaneously
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    await run_server()
    
    # Keep the asyncio loop running infinitely
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    asyncio.run(main())
