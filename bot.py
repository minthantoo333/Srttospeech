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

# --- 🗣️ VOICE, SPEED & RATE LIBRARIES ---
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
    "🤖 Auto-Sync (Recommended)": "auto",
    "1.0x (Normal)": 1.0,
    "0.9x (Slightly Slow)": 0.9,
    "0.8x (Slow)": 0.8,
    "0.7x (Very Slow)": 0.7
}

RATE_LIB = {
    "🐢 Slow (-10%)": "-10%",
    "🚶 Normal (0%)": "+0%",
    "🏃 Fast (+10%)": "+10%",
    "🏎️ Faster (+20%)": "+20%",
    "🚀 Very Fast (+30%)": "+30%"
}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "my-MM-NilarNeural",
            "video_speed": "auto", 
            "base_rate": "+20%" 
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
    seg = AudioSegment.from_file(file_path)
    seg = trim_silence(seg)
    seg.export(file_path, format="mp3")
    return len(seg)

def compose_final_audio(chunks_data, duration_ms, output_path):
    final_audio = AudioSegment.silent(duration=duration_ms + 2000) 
    
    for start_ms, file_path in chunks_data:
        try:
            segment = AudioSegment.from_file(file_path)
            segment = make_audio_crisp(segment)
            
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            
            final_audio = final_audio.overlay(segment, position=start_ms)
        except Exception as e:
            print(f"Error processing chunk {file_path}: {e}")
            
    final_audio.export(output_path, format="mp3", bitrate="192k")

# --- ⚡ CONCURRENT TASK WORKER (Base Rate + 70% Squeeze) ---
async def process_single_chunk(sub, index, user_id, voice, base_rate, semaphore, max_retries=3):
    async with semaphore:
        start_ms = sub.start.ordinal
        end_ms = sub.end.ordinal
        allowed_duration_ms = end_ms - start_ms
        
        text = sub.text.replace("\n", " ").strip()
        if not text: 
            return None 

        temp_filename = f"temp/{user_id}_chunk_{index}.mp3"
        PITCH_VAL = "-2Hz"
        
        # Safely parse the base rate string (e.g., "+20%") into an integer
        try:
            base_rate_int = int(base_rate.replace("%", "").replace("+", "").replace("-", ""))
            if "-" in base_rate: 
                base_rate_int = -base_rate_int
        except:
            base_rate_int = 20 
            
        current_rate_str = base_rate
        
        for attempt in range(max_retries):
            try:
                # 1. Generate audio at the user's chosen base rate
                communicate = edge_tts.Communicate(text, voice, rate=current_rate_str, pitch=PITCH_VAL)
                await communicate.save(temp_filename)
                
                # 2. Check the actual length
                current_len = await asyncio.to_thread(process_length_and_trim, temp_filename)
                
                ratio = 1.0
                if allowed_duration_ms > 0:
                    ratio = current_len / allowed_duration_ms

                # 3. 🚨 THE SQUEEZE MECHANISM (Fires to try and fit the gap)
                if ratio > 1.0:
                    extra_speed_needed = (ratio - 1) * 100
                    new_rate_int = int(base_rate_int + extra_speed_needed + 5)
                    
                    # Hard cap at +70% to prevent server errors/chipmunk audio
                    if new_rate_int > 70: 
                        new_rate_int = 70
                        
                    # Only regenerate if the squeeze rate is actually faster than base rate
                    if new_rate_int > base_rate_int:
                        current_rate_str = f"+{new_rate_int}%"
                        
                        communicate = edge_tts.Communicate(text, voice, rate=current_rate_str, pitch=PITCH_VAL)
                        await communicate.save(temp_filename)
                        current_len = await asyncio.to_thread(process_length_and_trim, temp_filename)
                        
                        # Recalculate ratio after max squeezing. 
                        # If it STILL overflows, Auto-Sync will handle the rest.
                        ratio = current_len / allowed_duration_ms

                return {
                    "index": index,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "file_path": temp_filename,
                    "ratio": ratio
                }
            except Exception as e:
                print(f"⚠️ Network error on chunk {index} (Attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to process chunk {index} after {max_retries} attempts.")
                await asyncio.sleep(1) 

# --- 🎬 DUBBING ENGINE ---
async def generate_dubbing(user_id, srt_path, output_path, voice, target_speed, base_rate):
    print(f"🎬 Starting Dubbing for {user_id} (Speed: {target_speed} | Rate: {base_rate})...")
    try:
        subs = pysrt.open(srt_path)
        if not subs:
            return False, "SRT file is empty.", "Unknown"

        max_ratio = 1.0 
        semaphore = asyncio.Semaphore(5) 
        tasks = []

        # PASS 1: Generate audio concurrently
        for i, sub in enumerate(subs):
            tasks.append(process_single_chunk(sub, i, user_id, voice, base_rate, semaphore))

        results = await asyncio.gather(*tasks)

        valid_results = [res for res in results if res is not None]
        valid_results.sort(key=lambda x: x["index"])

        chunks_data = []
        for res in valid_results:
            if res["ratio"] > max_ratio:
                max_ratio = res["ratio"]
            chunks_data.append((res["start_ms"], res["end_ms"], res["file_path"]))

        # --- APPLY SPEED LOGIC ---
        if target_speed == "auto":
            applied_ratio = max_ratio
            calculated_speed = round(1.0 / applied_ratio, 2)
            
            if calculated_speed < 0.6:
                print(f"⚠️ Capping Auto-Speed at 0.6x.")
                calculated_speed = 0.6
                applied_ratio = 1.0 / 0.6
                
            final_speed_reported = f"{calculated_speed}x (Auto)"
        else:
            # Manual Mode
            applied_ratio = 1.0 / float(target_speed)
            final_speed_reported = f"{target_speed}x (Manual)"

        # PASS 2: Calculate proportionately stretched timestamps
        stretched_chunks = []
        for start_ms, end_ms, file_path in chunks_data:
            new_start_ms = int(start_ms * applied_ratio)
            stretched_chunks.append((new_start_ms, file_path))

        last_sub_end_ms = int(subs[-1].end.ordinal * applied_ratio)

        await asyncio.to_thread(compose_final_audio, stretched_chunks, last_sub_end_ms, output_path)
        
        clean_temp(user_id)
        return True, None, final_speed_reported

    except Exception as e:
        clean_temp(user_id)
        return False, str(e), "Error"

# --- 🤖 HANDLERS ---
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("voices", "🗣️ Voice"),
        BotCommand("rate", "🎙️ Base Rate"),
        BotCommand("speed", "⏱️ Speed"),
        BotCommand("dub", "🎬 Dub"),
        BotCommand("clearall", "🧹 Clear")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    speed_val = state['video_speed']
    speed_name = next((k for k, v in SPEED_LIB.items() if v == speed_val), f"{speed_val}x")
    rate_val = state['base_rate']
    rate_name = next((k for k, v in RATE_LIB.items() if v == rate_val), rate_val)
    
    keyboard = [
        [InlineKeyboardButton(f"🗣️ Voice: {voice_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"🎙️ Speak Rate: {rate_name}", callback_data="cmd_rate")],
        [InlineKeyboardButton(f"⏱️ Speed: {speed_name}", callback_data="cmd_speed")]
    ]
    await update.message.reply_text(
        "👋 **SRT Dubbing Studio (Turbo Edition)**\n"
        "Send me an `.srt` file or paste SRT text to get started.", 
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

async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for name, val in RATE_LIB.items():
        row.append(InlineKeyboardButton(name, callback_data=f"set_rate_{val}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text("🎙️ **Select Base TTS Speak Rate:**\n*(How fast the AI talks naturally)*", reply_markup=InlineKeyboardMarkup(keyboard))

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
    await msg.reply_text("⏱️ **Select Video Speed Mode:**\n*(How timestamps stretch to prevent overlaps)*", reply_markup=InlineKeyboardMarkup(keyboard))

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wipe_user_data(update.effective_user.id)
    await update.message.reply_text("🧹 **All temporary files cleared.**")

async def dub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await perform_dubbing(update, context)

async def perform_dubbing(update, context):
    user_id = update.effective_user.id
    p = get_paths(user_id)
    state = get_user_state(user_id)
    
    if update.callback_query:
        msg = update.callback_query.message
    else:
        msg = update.message

    if not os.path.exists(p['srt']):
        await msg.reply_text("❌ **No SRT found. Please send an .srt file first.**")
        return

    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Selected Voice")
    speed_mode = state['video_speed']
    base_rate = state['base_rate']
    
    status = await msg.reply_text(f"🎬 **Dubbing ({voice_name})...**\n🎙️ Base Rate: `{base_rate}`\n⏱️ Speed Mode: `{speed_mode}`\nProcessing audio...")
    
    success, error, final_speed = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state['dub_voice'], speed_mode, base_rate)
    
    if success:
        await status.delete()
        caption = (
            f"✅ **Dubbed successfully!**\n"
            f"🗣️ Voice: {voice_name}\n"
            f"⚡ **Video Speed Applied: {final_speed}**"
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

    elif data == "cmd_speed":
        await speed_command(update, context)
        await query.answer()
        
    elif data == "cmd_rate":
        await rate_command(update, context)
        await query.answer()

    elif data.startswith("set_voice_"):
        new_voice = data.replace("set_voice_", "")
        state['dub_voice'] = new_voice
        v_name = next((k for k, v in VOICE_LIB.items() if v == new_voice), "Custom Voice")
        await query.message.edit_text(f"✅ Voice set to: **{v_name}**")

    elif data.startswith("set_rate_"):
        new_rate = data.replace("set_rate_", "")
        state['base_rate'] = new_rate
        r_name = next((k for k, v in RATE_LIB.items() if v == new_rate), new_rate)
        await query.message.edit_text(f"✅ Base Speak Rate set to: **{r_name}**")

    elif data.startswith("set_speed_"):
        val = data.replace("set_speed_", "")
        new_speed = "auto" if val == "auto" else float(val)
        state['video_speed'] = new_speed
        s_name = next((k for k, v in SPEED_LIB.items() if v == new_speed), f"{new_speed}x")
        await query.message.edit_text(f"✅ Target Video Speed set to: **{s_name}**")

    elif data == "trigger_dub":
        await perform_dubbing(update, context)
        await query.answer()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    text = msg.text
    p = get_paths(user_id)

    if re.search(r'\d{2}:\d{2}:\d{2},\d{3}', text):
        file_mode = 'a'
        if re.match(r'^\s*1\s*$', text.split('\n')[0].strip()) or text.strip().startswith('1\n'):
            file_mode = 'w'
        
        with open(p['srt'], file_mode, encoding="utf-8") as f: 
            f.write(text + "\n") 
        
        try:
            with open(p['srt'], 'r', encoding="utf-8") as f:
                raw_content = f.read()

            pattern = re.compile(r'(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\d+\s*\n\d{2}:\d{2}|\Z)', re.DOTALL)
            matches = pattern.findall(raw_content)

            clean_srt = ""
            for match in matches:
                index = match[0].strip()
                timestamp = match[1].strip()
                sub_text = "\n".join([line for line in match[2].split('\n') if line.strip()])
                clean_srt += f"{index}\n{timestamp}\n{sub_text}\n\n"

            with open(p['srt'], 'w', encoding="utf-8") as f:
                f.write(clean_srt)
                
        except Exception as e:
            print(f"Cleaner Error: {e}")

        try:
            subs = pysrt.open(p['srt'])
            if len(subs) > 0:
                total_lines = len(subs)
                latest_end = subs[-1].end
                time_str = f"{latest_end.hours:02d}:{latest_end.minutes:02d}:{latest_end.seconds:02d},{latest_end.milliseconds:03d}"
                
                reply_text = (
                    f"✅ **SRT Text Saved & Auto-Cleaned!**\n"
                    f"📊 **Total Valid Blocks:** {total_lines}\n"
                    f"⏱️ **Latest Timestamp:** {time_str}\n\n"
                    f"*(Paste more to append, or click below to start)*"
                )
            else:
                reply_text = "⚠️ Text saved, but no valid subtitle blocks could be extracted. Please check the formatting."
        except Exception as e:
            reply_text = f"⚠️ Text saved, but parsing failed: {e}"

        keyboard = [[InlineKeyboardButton("🎬 Dub Audio", callback_data="trigger_dub")]]
        await msg.reply_text(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
    else:
        await msg.reply_text("ℹ️ Please send an `.srt` file or paste valid SRT text format containing timestamps.")

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
    print("🚀 Initializing Concurrent SRT Dubbing Bot...")
    
    bot_app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("voices", voices_command))
    bot_app.add_handler(CommandHandler("rate", rate_command))
    bot_app.add_handler(CommandHandler("speed", speed_command))
    bot_app.add_handler(CommandHandler("dub", dub_command))
    bot_app.add_handler(CommandHandler("clearall", clearall_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    asyncio.create_task(run_server())
    
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    asyncio.run(main())
