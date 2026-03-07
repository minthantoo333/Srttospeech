import os
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

# --- 🗣️ VOICE LIBRARY ---
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

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        # Default voice is Burmese Female
        user_prefs[user_id] = {
            "dub_voice": "my-MM-NilarNeural"
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
    return audio_segment[start_trim:len(audio_segment)-end_trim]

def make_audio_crisp(audio_segment):
    clean = audio_segment.high_pass_filter(150)
    return effects.normalize(clean)

def process_length_and_trim(file_path):
    """Loads, trims, overwrites, and returns length of audio (Runs in thread)"""
    seg = AudioSegment.from_file(file_path)
    seg = trim_silence(seg)
    seg.export(file_path, format="mp3")
    return len(seg)

def compose_final_audio(chunks_data, duration_ms, output_path):
    """Stitches all generated chunks onto a silent canvas (Runs in thread)"""
    final_audio = AudioSegment.silent(duration=duration_ms + 5000) 
    
    for start_ms, file_path in chunks_data:
        try:
            segment = AudioSegment.from_file(file_path)
            segment = make_audio_crisp(segment)
            
            # Dynamic Canvas Extension
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            
            final_audio = final_audio.overlay(segment, position=start_ms)
        except Exception as e:
            print(f"Error processing chunk {file_path}: {e}")
            
    final_audio = trim_silence(final_audio)
    final_audio.export(output_path, format="mp3", bitrate="192k")

# --- 🎬 DUBBING ENGINE (AUTO-SPEED) ---
async def generate_dubbing(user_id, srt_path, output_path, voice):
    print(f"🎬 Starting Auto-Speed Dubbing for {user_id}...")
    try:
        subs = pysrt.open(srt_path)
        if not subs:
            return False, "SRT file is empty.", 1.0

        chunks_data = []
        # +20% represents a 1.2x base reading speed in edge-tts
        BASE_RATE_VAL = "+20%" 
        PITCH_VAL = "-2Hz"
        max_ratio = 1.0 

        # PASS 1: Generate audio and find the maximum required stretch
        for i, sub in enumerate(subs):
            start_ms = sub.start.ordinal
            end_ms = sub.end.ordinal
            allowed_duration_ms = end_ms - start_ms
            
            text = sub.text.replace("\n", " ").strip()
            if not text: continue 

            temp_filename = f"temp/{user_id}_chunk_{i}.mp3"
            
            # Generate TTS audio at 1.2x base rate
            communicate = edge_tts.Communicate(text, voice, rate=BASE_RATE_VAL, pitch=PITCH_VAL)
            await communicate.save(temp_filename)
            
            # Check actual audio length in background thread
            current_len = await asyncio.to_thread(process_length_and_trim, temp_filename)

            # Calculate ratio if this specific audio exceeds its allowed SRT duration
            if allowed_duration_ms > 0:
                ratio = current_len / allowed_duration_ms
                if ratio > max_ratio:
                    max_ratio = ratio

            chunks_data.append((start_ms, end_ms, temp_filename))

        # Calculate the final global auto-speed
        auto_speed = round(1.0 / max_ratio, 2)
        
        # SAFETY CAP: Prevent the video from becoming unreasonably slow due to one outlier
        if auto_speed < 0.6:
            print(f"⚠️ Warning: Calculated speed {auto_speed}x is too slow. Capping at 0.6x.")
            auto_speed = 0.6
            max_ratio = 1.0 / 0.6

        # PASS 2: Calculate proportionately stretched timestamps
        stretched_chunks = []
        for start_ms, end_ms, file_path in chunks_data:
            new_start_ms = int(start_ms * max_ratio)
            stretched_chunks.append((new_start_ms, file_path))

        last_sub_end_ms = int(subs[-1].end.ordinal * max_ratio)

        # Offload the heavy audio stitching to a background thread
        await asyncio.to_thread(compose_final_audio, stretched_chunks, last_sub_end_ms, output_path)
        
        clean_temp(user_id)
        return True, None, auto_speed

    except Exception as e:
        clean_temp(user_id)
        return False, str(e), 1.0

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
    await update.message.reply_text(
        "👋 **SRT Dubbing Studio (Auto-Sync Edition)**\n"
        "Send me an `.srt` file or paste SRT text to get started.\n"
        "*(Timestamps will stretch automatically to prevent overlaps)*", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
    
    # Handle both direct commands and button clicks
    if update.callback_query:
        msg = update.callback_query.message
    else:
        msg = update.message

    if not os.path.exists(p['srt']):
        await msg.reply_text("❌ **No SRT found. Please send an .srt file first.**")
        return

    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Selected Voice")
    
    status = await msg.reply_text(f"🎬 **Dubbing ({voice_name})...**\nAnalyzing subtitle lengths to calculate Auto-Speed. Please wait...")
    
    success, error, calculated_speed = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state['dub_voice'])
    
    if success:
        await status.delete()
        caption = (
            f"✅ **Dubbed successfully!**\n"
            f"🗣️ Voice: {voice_name}\n"
            f"⚡ **Auto-Applied Video Speed: {calculated_speed}x**"
        )
        await context.bot.send_audio(
            chat_id=msg.chat_id, 
            audio=open(p['dub_audio'], "rb"), 
            caption=caption
        )
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
    bot_app.add_handler(CommandHandler("dub", dub_command))
    bot_app.add_handler(CommandHandler("clearall", clearall_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    # Start aiohttp server in the background
    asyncio.create_task(run_server())
    
    # Keep the bot running
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    asyncio.run(main())
