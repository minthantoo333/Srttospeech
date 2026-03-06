import os
import io
import glob
import asyncio
import pysrt
import re 
from aiohttp import web

# Audio Processing
from pydub import AudioSegment, effects
from pydub.silence import detect_leading_silence
import edge_tts

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters

# --- ⚙️ CONFIGURATION ---
TG_TOKEN = os.getenv("TG_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

if not TG_TOKEN:
    print("❌ ERROR: TG_TOKEN is missing! Set it in your environment variables.")
    exit()

# --- 🗣️ VOICE & SPEED LIBRARY ---
VOICE_LIB = {
    "🇯🇵 Japanese (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Japanese (Male)": "ja-JP-KeitaNeural",
    "🇲🇲 Burmese (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Burmese (Female)": "my-MM-NilarNeural", 
    "🇺🇸 Remy (Multi)": "en-US-RemyMultilingualNeural",
    "🇮🇹 Giuseppe (Multi)": "it-IT-GiuseppeMultilingualNeural",
    "🇺🇸 Brian (Male)": "en-US-BrianNeural",
    "🇺🇸 Andrew (Male)": "en-US-AndrewNeural"
}

SPEED_LIB = {
    "1.0x (Normal)": 1.0,
    "0.9x (Slightly Slow)": 0.9,
    "0.8x (Slow)": 0.8,
    "0.7x (Very Slow)": 0.7
}

BASE_FOLDERS = ["downloads"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "my-MM-NilarNeural", 
            "video_speed": 1.0 
        }
    return user_prefs[user_id]

def get_paths(user_id):
    return {
        "srt": f"downloads/{user_id}_subs.srt",
        "dub_audio": f"downloads/{user_id}_dubbed.mp3"
    }

def wipe_user_data(user_id):
    for f in glob.glob(f"downloads/{user_id}_*"):
        try: os.remove(f)
        except: pass
    if user_id in user_prefs: del user_prefs[user_id]

# --- 🔊 AUDIO POST-PROCESSING (In-Memory) ---
def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100:  
        return audio_segment
    start_trim = detect_leading_silence(audio_segment, silence_threshold=silence_thresh, chunk_size=chunk_size)
    end_trim = detect_leading_silence(audio_segment.reverse(), silence_threshold=silence_thresh, chunk_size=chunk_size)
    return audio_segment[start_trim:len(audio_segment)-end_trim]

def make_audio_crisp(audio_segment):
    clean = audio_segment.high_pass_filter(150)
    return effects.normalize(clean)

def process_audio_segment(audio_bytes_io):
    """Loads from BytesIO, trims, and returns the AudioSegment (Runs in thread)"""
    seg = AudioSegment.from_file(audio_bytes_io, format="mp3")
    return trim_silence(seg)

def compose_final_audio(chunks_data, duration_ms, output_path):
    """Stitches all generated AudioSegments onto a silent canvas (Runs in thread)"""
    final_audio = AudioSegment.silent(duration=duration_ms + 5000) 
    
    for start_ms, segment in chunks_data:
        try:
            segment = make_audio_crisp(segment)
            
            # Dynamic Canvas Extension
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            
            final_audio = final_audio.overlay(segment, position=start_ms)
        except Exception as e:
            print(f"Error processing chunk: {e}")
            
    final_audio = trim_silence(final_audio)
    final_audio.export(output_path, format="mp3", bitrate="192k")

# --- 🎬 ASYNC DUBBING ENGINE ---
async def process_single_sub(sub, user_id, voice, time_multiplier, sem):
    """Processes a single subtitle entirely in RAM. Semaphore limits concurrent API hits."""
    async with sem:
        start_ms = int(sub.start.ordinal * time_multiplier)
        end_ms = int(sub.end.ordinal * time_multiplier)
        allowed_duration_ms = end_ms - start_ms
        
        text = sub.text.replace("\n", " ").strip()
        if not text: 
            return None 

        BASE_RATE_VAL = 10 
        PITCH_VAL = "-2Hz"

        async def get_audio_bytes(rate_val):
            """Streams Edge TTS directly into RAM (BytesIO)"""
            comm = edge_tts.Communicate(text, voice, rate=f"+{rate_val}%", pitch=PITCH_VAL)
            bio = io.BytesIO()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    bio.write(chunk["data"])
            bio.seek(0)
            return bio

        # 1. Generate normal TTS audio directly into memory
        bio = await get_audio_bytes(BASE_RATE_VAL)
        
        # 2. Check length in background thread
        seg = await asyncio.to_thread(process_audio_segment, bio)
        current_len = len(seg)

        # 3. Speed up if still too long for the new stretched window
        if current_len > allowed_duration_ms:
            ratio = current_len / allowed_duration_ms
            extra_speed_needed = (ratio - 1) * 100
            new_rate = int(BASE_RATE_VAL + extra_speed_needed + 5)
            if new_rate > 70: new_rate = 70 
            
            bio = await get_audio_bytes(new_rate)
            seg = await asyncio.to_thread(process_audio_segment, bio)

        return (start_ms, seg)

async def generate_dubbing(user_id, srt_path, output_path, voice, target_speed):
    print(f"🎬 Starting Dubbing for {user_id} at {target_speed}x speed...")
    try:
        subs = pysrt.open(srt_path)
        if not subs:
            return False, "SRT file is empty."

        time_multiplier = 1.0 / target_speed
        last_sub_end_ms = int(subs[-1].end.ordinal * time_multiplier)
        
        # Limit to 15 concurrent tasks so we don't overwhelm Edge TTS or RAM
        sem = asyncio.Semaphore(15) 
        tasks = []

        for sub in subs:
            tasks.append(process_single_sub(sub, user_id, voice, time_multiplier, sem))

        # Run all TTS requests concurrently
        results = await asyncio.gather(*tasks)
        
        # Filter out skipped (empty) subtitles
        chunks_data = [res for res in results if res is not None]

        # 4. Offload the heavy audio stitching to a background thread
        await asyncio.to_thread(compose_final_audio, chunks_data, last_sub_end_ms, output_path)
        
        return True, None

    except Exception as e:
        return False, str(e)

# --- 🤖 HANDLERS ---
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("voices", "🗣️ Change Voice"),
        BotCommand("speed", "⏱️ Video Speed"),
        BotCommand("dub", "🎬 Dub Audio"),
        BotCommand("clearall", "🧹 Clear Data")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    speed_val = state['video_speed']
    
    keyboard = [
        [InlineKeyboardButton(f"🗣️ Voice: {voice_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"⏱️ Target Video Speed: {speed_val}x", callback_data="cmd_speed")]
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

async def speed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for name, val in SPEED_LIB.items():
        row.append(InlineKeyboardButton(name, callback_data=f"set_speed_{val}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text("⏱️ **Select your target Video Speed:**\n(Timestamps will stretch automatically to fit the slower video)", reply_markup=InlineKeyboardMarkup(keyboard))

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wipe_user_data(update.effective_user.id)
    await update.message.reply_text("🧹 **All data cleared.**")

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
    speed_val = state['video_speed']
    status = await msg.reply_text(f"🎬 **Dubbing ({voice_name} | {speed_val}x Speed)...**\nPlease wait, this is processing concurrently.")
    
    success, error = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state['dub_voice'], speed_val)
    
    if success:
        await status.delete()
        with open(p['dub_audio'], "rb") as audio_file:
            await context.bot.send_audio(chat_id=msg.chat_id, audio=audio_file, caption=f"✅ **Dubbed successfully!**\n🗣️ Voice: {voice_name}\n⏱️ Optimized for: {speed_val}x Video Speed")
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

    elif data == "cmd_speed":
        await speed_command(update, context)
        await query.answer()

    elif data.startswith("set_voice_"):
        new_voice = data.replace("set_voice_", "")
        state['dub_voice'] = new_voice
        v_name = next((k for k, v in VOICE_LIB.items() if v == new_voice), "Custom Voice")
        await query.message.edit_text(f"✅ Voice set to: **{v_name}**")

    elif data.startswith("set_speed_"):
        new_speed = float(data.replace("set_speed_", ""))
        state['video_speed'] = new_speed
        await query.message.edit_text(f"✅ Target Video Speed set to: **{new_speed}x**")

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
    return web.Response(text="Bot is running smoothly!", status=200)

async def run_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"🌐 Uptime Web Server running on port {PORT}")

# --- 🚀 MAIN LOOP ---
async def main():
    print("🚀 Initializing SRT Dubbing Bot...")
    
    bot_app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("voices", voices_command))
    bot_app.add_handler(CommandHandler("speed", speed_command))
    bot_app.add_handler(CommandHandler("dub", dub_command))
    bot_app.add_handler(CommandHandler("clearall", clearall_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    await run_server()
    
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    asyncio.run(main())
