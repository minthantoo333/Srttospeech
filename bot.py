import os
import glob
import asyncio
import pysrt
import re 
import time
import math
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

# --- 🗣️ LIBRARIES ---
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

FIT_LIB = {
    "🗜️ Max Fit +50%": 50,
    "🗜️ Max Fit +60%": 60,
    "🗜️ Max Fit +70%": 70
}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "my-MM-ThihaNeural", # Changed default to Thiha
            "video_speed": "auto", 
            "base_rate": "+20%",
            "max_fit": 70 # Default max limit for the squeeze
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

# --- ⚡ CONCURRENT TASK WORKER ---
async def process_single_chunk(sub, index, user_id, voice, base_rate, max_fit, semaphore, progress_cb, max_retries=3):
    async with semaphore:
        start_ms = sub.start.ordinal
        end_ms = sub.end.ordinal
        allowed_duration_ms = end_ms - start_ms
        
        text = sub.text.replace("\n", " ").strip()
        if not text: 
            await progress_cb()
            return None 

        temp_filename = f"temp/{user_id}_chunk_{index}.mp3"
        PITCH_VAL = "-2Hz"
        
        try:
            base_rate_int = int(base_rate.replace("%", "").replace("+", "").replace("-", ""))
            if "-" in base_rate: 
                base_rate_int = -base_rate_int
        except:
            base_rate_int = 20 
            
        current_rate_str = base_rate
        
        for attempt in range(max_retries):
            try:
                communicate = edge_tts.Communicate(text, voice, rate=current_rate_str, pitch=PITCH_VAL)
                await communicate.save(temp_filename)
                
                current_len = await asyncio.to_thread(process_length_and_trim, temp_filename)
                
                ratio = 1.0
                if allowed_duration_ms > 0:
                    ratio = current_len / allowed_duration_ms

                # 🚨 THE SQUEEZE MECHANISM (Uses user-selected max_fit limit)
                if ratio > 1.0:
                    extra_speed_needed = (ratio - 1) * 100
                    new_rate_int = int(base_rate_int + extra_speed_needed + 5)
                    
                    if new_rate_int > max_fit: 
                        new_rate_int = max_fit
                        
                    if new_rate_int > base_rate_int:
                        current_rate_str = f"+{new_rate_int}%"
                        communicate = edge_tts.Communicate(text, voice, rate=current_rate_str, pitch=PITCH_VAL)
                        await communicate.save(temp_filename)
                        current_len = await asyncio.to_thread(process_length_and_trim, temp_filename)
                        ratio = current_len / allowed_duration_ms

                await progress_cb() # Update Telegram UI
                return {
                    "index": index,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "file_path": temp_filename,
                    "ratio": ratio
                }
            except Exception as e:
                print(f"⚠️ Network error chunk {index}: {e}")
                if attempt == max_retries - 1:
                    raise Exception(f"Failed chunk {index} after {max_retries} attempts.")
                await asyncio.sleep(1) 

# --- 🎬 DUBBING ENGINE ---
async def generate_dubbing(user_id, srt_path, output_path, state, status_msg):
    try:
        subs = pysrt.open(srt_path)
        if not subs: return False, "SRT file is empty.", "Unknown"

        voice = state['dub_voice']
        base_rate = state['base_rate']
        target_speed = state['video_speed']
        max_fit = state['max_fit']
        
        total_chunks = len(subs)
        completed_chunks = 0
        last_update_time = time.time()

        # UI Progress Callback Function
        async def update_progress():
            nonlocal completed_chunks, last_update_time
            completed_chunks += 1
            now = time.time()
            # Only edit telegram message every 2 seconds to avoid API bans, or at 100%
            if now - last_update_time > 2.0 or completed_chunks == total_chunks:
                percent = int((completed_chunks / total_chunks) * 100)
                try:
                    await status_msg.edit_text(
                        f"🎬 **Dubbing...**\n"
                        f"🎙️ Base: `{base_rate}` | 🗜️ Max Fit: `+{max_fit}%`\n"
                        f"⏱️ Mode: `{target_speed}`\n\n"
                        f"⏳ **Processing: {percent}%** ({completed_chunks}/{total_chunks})"
                    )
                    last_update_time = now
                except Exception:
                    pass

        max_ratio = 1.0 
        semaphore = asyncio.Semaphore(5) 
        tasks = []

        for i, sub in enumerate(subs):
            tasks.append(process_single_chunk(sub, i, user_id, voice, base_rate, max_fit, semaphore, update_progress))

        results = await asyncio.gather(*tasks)

        valid_results = [res for res in results if res is not None]
        valid_results.sort(key=lambda x: x["index"])

        chunks_data = []
        for res in valid_results:
            if res["ratio"] > max_ratio: max_ratio = res["ratio"]
            chunks_data.append((res["start_ms"], res["end_ms"], res["file_path"]))

        # --- APPLY CLEAN 1-DECIMAL MATH ---
        if target_speed == "auto":
            raw_calculated_speed = 1.0 / max_ratio
            
            # Floor to the nearest 0.1 (e.g. 0.88 becomes 0.8) to guarantee NO overlap
            clean_speed = math.floor(raw_calculated_speed * 10) / 10.0
            
            if clean_speed < 0.6: clean_speed = 0.6
                
            applied_ratio = 1.0 / clean_speed
            final_speed_reported = f"{clean_speed}x (Auto)"
        else:
            applied_ratio = 1.0 / float(target_speed)
            final_speed_reported = f"{target_speed}x (Manual)"

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
        BotCommand("fit", "🗜️ Max Fit Speed"),
        BotCommand("speed", "⏱️ Video Speed"),
        BotCommand("dub", "🎬 Dub"),
        BotCommand("clearall", "🧹 Clear")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    
    voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Unknown")
    speed_name = next((k for k, v in SPEED_LIB.items() if v == state['video_speed']), f"{state['video_speed']}x")
    rate_name = next((k for k, v in RATE_LIB.items() if v == state['base_rate']), state['base_rate'])
    fit_name = f"+{state['max_fit']}%"
    
    keyboard = [
        [InlineKeyboardButton(f"🗣️ Voice: {voice_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"🎙️ Rate: {rate_name}", callback_data="cmd_rate"), InlineKeyboardButton(f"🗜️ Max Fit: {fit_name}", callback_data="cmd_fit")],
        [InlineKeyboardButton(f"⏱️ Video Speed: {speed_name}", callback_data="cmd_speed")]
    ]
    await update.message.reply_text(
        "👋 **SRT Dubbing Studio (Turbo UI Edition)**\n"
        "Send me an `.srt` file or paste SRT text to get started.", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_menu(update, context, lib, prefix, text_msg):
    keyboard = []
    row = []
    for name, val in lib.items():
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}_{val}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text(text_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def voices_command(u, c): await handle_menu(u, c, VOICE_LIB, "set_voice", "🗣️ **Select Narrator Voice:**")
async def rate_command(u, c): await handle_menu(u, c, RATE_LIB, "set_rate", "🎙️ **Select Base TTS Speak Rate:**")
async def fit_command(u, c): await handle_menu(u, c, FIT_LIB, "set_fit", "🗜️ **Select Max Emergency Squeeze Speed:**\n*(Limits how fast the AI is allowed to talk to fit a gap)*")
async def speed_command(u, c): await handle_menu(u, c, SPEED_LIB, "set_speed", "⏱️ **Select Video Speed Mode:**\n*(Auto mathematically prevents overlaps)*")

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wipe_user_data(update.effective_user.id)
    await update.message.reply_text("🧹 **All temporary files cleared.**")

async def dub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await perform_dubbing(update, context)

async def perform_dubbing(update, context):
    user_id = update.effective_user.id
    p = get_paths(user_id)
    state = get_user_state(user_id)
    msg = update.callback_query.message if update.callback_query else update.message

    if not os.path.exists(p['srt']):
        await msg.reply_text("❌ **No SRT found. Please send an .srt file first.**")
        return

    status_msg = await msg.reply_text("🎬 **Dubbing Initializing...**\n⏳ **Processing: 0%**")
    
    success, error, final_speed = await generate_dubbing(user_id, p['srt'], p['dub_audio'], state, status_msg)
    
    if success:
        voice_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Selected Voice")
        await status_msg.delete()
        caption = (
            f"✅ **Dubbed successfully!**\n"
            f"🗣️ Voice: {voice_name}\n"
            f"⚡ **Clean Video Speed Applied: {final_speed}**"
        )
        await context.bot.send_audio(chat_id=msg.chat_id, audio=open(p['dub_audio'], "rb"), caption=caption)
    else:
        await status_msg.edit_text(f"❌ Dubbing Failed: {error}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data
    
    if data == "cmd_voices": await voices_command(update, context)
    elif data == "cmd_speed": await speed_command(update, context)
    elif data == "cmd_rate": await rate_command(update, context)
    elif data == "cmd_fit": await fit_command(update, context)
    elif data == "trigger_dub": await perform_dubbing(update, context)
    
    elif data.startswith("set_voice_"):
        state['dub_voice'] = data.replace("set_voice_", "")
        v_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Voice")
        await query.message.edit_text(f"✅ Voice set to: **{v_name}**")

    elif data.startswith("set_rate_"):
        state['base_rate'] = data.replace("set_rate_", "")
        r_name = next((k for k, v in RATE_LIB.items() if v == state['base_rate']), state['base_rate'])
        await query.message.edit_text(f"✅ Base Speak Rate set to: **{r_name}**")

    elif data.startswith("set_fit_"):
        state['max_fit'] = int(data.replace("set_fit_", ""))
        await query.message.edit_text(f"✅ Max Fit Limit set to: **+{state['max_fit']}%**")

    elif data.startswith("set_speed_"):
        val = data.replace("set_speed_", "")
        state['video_speed'] = "auto" if val == "auto" else float(val)
        s_name = next((k for k, v in SPEED_LIB.items() if v == state['video_speed']), f"{state['video_speed']}x")
        await query.message.edit_text(f"✅ Target Video Speed set to: **{s_name}**")

    await query.answer()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    text = msg.text
    p = get_paths(user_id)

    if re.search(r'\d{2}:\d{2}:\d{2},\d{3}', text):
        file_mode = 'w' if re.match(r'^\s*1\s*$', text.split('\n')[0].strip()) or text.strip().startswith('1\n') else 'a'
        with open(p['srt'], file_mode, encoding="utf-8") as f: f.write(text + "\n") 
        
        try:
            with open(p['srt'], 'r', encoding="utf-8") as f: raw_content = f.read()
            pattern = re.compile(r'(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\d+\s*\n\d{2}:\d{2}|\Z)', re.DOTALL)
            clean_srt = "".join([f"{m[0].strip()}\n{m[1].strip()}\n{chr(10).join([L for L in m[2].split(chr(10)) if L.strip()])}\n\n" for m in pattern.findall(raw_content)])
            with open(p['srt'], 'w', encoding="utf-8") as f: f.write(clean_srt)
        except Exception as e: print(f"Cleaner Error: {e}")

        try:
            subs = pysrt.open(p['srt'])
            if len(subs) > 0:
                end = subs[-1].end
                time_str = f"{end.hours:02d}:{end.minutes:02d}:{end.seconds:02d},{end.milliseconds:03d}"
                reply_text = f"✅ **SRT Text Validated!**\n📊 **Total:** {len(subs)}\n⏱️ **End:** {time_str}\n\n*(Paste more or click start)*"
            else:
                reply_text = "⚠️ Text saved, but no blocks detected."
        except Exception as e:
            reply_text = f"⚠️ Parsing failed: {e}"

        keyboard = [[InlineKeyboardButton("🎬 Dub Audio", callback_data="trigger_dub")]]
        await msg.reply_text(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.reply_text("ℹ️ Please send an `.srt` file or paste valid SRT text.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)
    if msg.document.file_name.lower().endswith('.srt'):
        await (await msg.document.get_file()).download_to_drive(p['srt'])
        await msg.reply_text("✅ **SRT File Loaded.** Ready to dub!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Dub Audio", callback_data="trigger_dub")]]))
    else:
        await msg.reply_text("❌ Please send an `.srt` file.")

async def health_check(request): return web.Response(text="Bot running!", status=200)

async def run_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    print(f"🌐 Server on {PORT}")

async def main():
    bot_app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()
    for cmd, hndlr in [("start", start), ("voices", voices_command), ("rate", rate_command), ("fit", fit_command), ("speed", speed_command), ("dub", dub_command), ("clearall", clearall_command)]:
        bot_app.add_handler(CommandHandler(cmd, hndlr))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    asyncio.create_task(run_server())
    await asyncio.Event().wait()

if __name__ == '__main__': asyncio.run(main())
