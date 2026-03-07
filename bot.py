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
    "🇲🇲 Thiha (Male)": "my-MM-ThihaNeural",
    "🇲🇲 Nilar (Female)": "my-MM-NilarNeural", 
    "🇺🇸 Remy (Multi)": "en-US-RemyMultilingualNeural",
    "🇺🇸 Andrew (Multi)": "en-US-AndrewMultilingualNeural",
    "🇺🇸 Brian (Multi)": "en-US-BrianMultilingualNeural",
    "🇺🇸 Emma (Multi)": "en-US-EmmaMultilingualNeural",
    "🇺🇸 Ava (Multi)": "en-US-AvaMultilingualNeural",
    "🇮🇹 Giuseppe (Multi)": "it-IT-GiuseppeMultilingualNeural",
    "🇫🇷 Vivienne (Multi)": "fr-FR-VivienneMultilingualNeural",
    "🇩🇪 Florian (Multi)": "de-DE-FlorianMultilingualNeural",
    "🇩🇪 Seraphina (Multi)": "de-DE-SeraphinaMultilingualNeural",
    "🇯🇵 Nanami (Female)": "ja-JP-NanamiNeural",
    "🇯🇵 Keita (Male)": "ja-JP-KeitaNeural",
    "🇰🇷 SunHi (Female)": "ko-KR-SunHiNeural"
}

# 🚀 MULTI-CHARACTER ALIASES (E.g., "nilar: မင်္ဂလာပါ" will use Nilar's voice)
SPEAKER_ALIASES = {
    "thiha": "my-MM-ThihaNeural",
    "nilar": "my-MM-NilarNeural",
    "remy": "en-US-RemyMultilingualNeural",
    "andrew": "en-US-AndrewMultilingualNeural",
    "brian": "en-US-BrianMultilingualNeural",
    "emma": "en-US-EmmaMultilingualNeural",
    "ava": "en-US-AvaMultilingualNeural",
    "giuseppe": "it-IT-GiuseppeMultilingualNeural",
    "vivienne": "fr-FR-VivienneMultilingualNeural",
    "florian": "de-DE-FlorianMultilingualNeural",
    "seraphina": "de-DE-SeraphinaMultilingualNeural",
    "nanami": "ja-JP-NanamiNeural",
    "keita": "ja-JP-KeitaNeural",
    "sunhi": "ko-KR-SunHiNeural"
}

SPEED_LIB = {
    "🤖 Auto-Sync (Rec.)": "auto",
    "1.0x (Normal)": 1.0,
    "0.9x (Slow)": 0.9,
    "0.8x (Slower)": 0.8,
    "0.7x (Very Slow)": 0.7
}

RATE_LIB = {
    "🐢 -10%": "-10%",
    "🚶 +0%": "+0%",
    "🏃 +10%": "+10%",
    "🏎️ +20%": "+20%",
    "🚀 +30%": "+30%"
}

FIT_LIB = {
    "🗜️ Max +50%": 50,
    "🗜️ Max +60%": 60,
    "🗜️ Max +70%": 70
}

BASE_FOLDERS = ["downloads", "temp"]
for f in BASE_FOLDERS:
    os.makedirs(f, exist_ok=True)

user_prefs = {}

# --- 🛠️ HELPER FUNCTIONS ---
def get_user_state(user_id):
    if user_id not in user_prefs:
        user_prefs[user_id] = {
            "dub_voice": "my-MM-ThihaNeural", 
            "video_speed": "auto", 
            "base_rate": "+20%",
            "max_fit": 70 
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

def parse_speaker(text, default_voice):
    """Detects 'Name: Text' format and returns the matching voice and clean text."""
    match = re.match(r"^([a-zA-Z0-9]+)\s*:\s*(.*)", text)
    if match:
        speaker_name = match.group(1).lower()
        clean_text = match.group(2)
        if speaker_name in SPEAKER_ALIASES:
            return SPEAKER_ALIASES[speaker_name], clean_text
    return default_voice, text

# --- 🔊 AUDIO POST-PROCESSING ---
def trim_silence(audio_segment, silence_thresh=-40.0, chunk_size=5):
    if len(audio_segment) < 100:  return audio_segment
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
        if not file_path: continue
        try:
            segment = AudioSegment.from_file(file_path)
            segment = make_audio_crisp(segment)
            if start_ms + len(segment) > len(final_audio):
                final_audio += AudioSegment.silent(duration=(start_ms + len(segment) - len(final_audio) + 2000))
            final_audio = final_audio.overlay(segment, position=start_ms)
        except Exception as e:
            print(f"Error chunk {file_path}: {e}")
    final_audio.export(output_path, format="mp3", bitrate="192k")

# --- ⚡ PHASE 1: MEASURE BASE AUDIO ---
async def measure_base_chunk(chunk, user_id, base_rate_str, semaphore, progress_cb):
    async with semaphore:
        idx = chunk["index"]
        text = chunk["text"]
        voice = chunk["voice"]
        if not text: 
            await progress_cb()
            return {"index": idx, "len": 0, "path": None, "text": "", "voice": voice}
            
        path = f"temp/{user_id}_chunk_{idx}_base.mp3"
        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, voice, rate=base_rate_str, pitch="-2Hz")
                await comm.save(path)
                length = await asyncio.to_thread(process_length_and_trim, path)
                await progress_cb()
                return {"index": idx, "len": length, "path": path, "text": text, "voice": voice}
            except Exception:
                if attempt == 2: return {"index": idx, "len": 0, "path": None, "text": "", "voice": voice}
                await asyncio.sleep(1)

# --- ⚡ PHASE 2: FINAL PERFECT FIT GENERATION ---
async def process_final_chunk(chunk, base_res, global_ratio, user_id, base_rate_int, max_fit, semaphore, progress_cb):
    async with semaphore:
        idx = chunk["index"]
        start_ms = chunk["start"]
        stretched_gap = chunk["gap"] * global_ratio
        new_start_ms = int(start_ms * global_ratio)

        if not base_res or base_res["len"] == 0:
            await progress_cb()
            return (new_start_ms, None)

        base_len = base_res["len"]
        base_path = base_res["path"]
        text = base_res["text"]
        voice = base_res["voice"]

        if base_len <= stretched_gap:
            await progress_cb()
            return (new_start_ms, base_path) 
            
        ratio = base_len / stretched_gap
        extra_speed = (ratio - 1) * 100
        new_rate_int = int(base_rate_int + extra_speed + 5) 
        if new_rate_int > max_fit: new_rate_int = max_fit

        final_path = f"temp/{user_id}_chunk_{idx}_final.mp3"
        rate_str = f"+{new_rate_int}%"
        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, voice, rate=rate_str, pitch="-2Hz")
                await comm.save(final_path)
                await asyncio.to_thread(process_length_and_trim, final_path)
                await progress_cb()
                return (new_start_ms, final_path)
            except Exception:
                if attempt == 2: 
                    await progress_cb()
                    return (new_start_ms, base_path) 
                await asyncio.sleep(1)

# --- 🎬 ENGINE (DUB OR ANALYZE) ---
async def process_engine(user_id, srt_path, output_path, state, status_msg, analyze_only=False):
    try:
        subs = pysrt.open(srt_path)
        if not subs: return False, "SRT file is empty.", "Unknown", "Unknown"

        global_voice = state['dub_voice']
        base_rate_str = state['base_rate']
        target_speed = state['video_speed']
        max_fit = state['max_fit']
        
        try:
            base_rate_int = int(base_rate_str.replace("%", "").replace("+", "").replace("-", ""))
            if "-" in base_rate_str: base_rate_int = -base_rate_int
        except: base_rate_int = 20

        base_speed_factor = 1.0 + (base_rate_int / 100.0)
        max_speed_factor = 1.0 + (max_fit / 100.0)

        chunks_data = []
        for i, sub in enumerate(subs):
            start_ms = sub.start.ordinal
            end_ms = sub.end.ordinal
            next_start_ms = subs[i+1].start.ordinal if i + 1 < len(subs) else end_ms + 2000 
            gap_duration = next_start_ms - start_ms
            
            raw_text = sub.text.replace("\n", " ").strip()
            # 🚀 Detect Multi-Character Voice here!
            chunk_voice, clean_text = parse_speaker(raw_text, global_voice)
            
            chunks_data.append({"index": i, "start": start_ms, "end": end_ms, "gap": gap_duration, "text": clean_text, "voice": chunk_voice})

        total_tasks = len(chunks_data) if analyze_only else len(chunks_data) * 2 
        completed_tasks = 0
        last_update_time = time.time()

        async def update_progress(phase_name):
            nonlocal completed_tasks, last_update_time
            completed_tasks += 1
            now = time.time()
            if now - last_update_time > 2.0 or completed_tasks == total_tasks:
                percent = int((completed_tasks / total_tasks) * 100)
                icon = "🔍" if analyze_only else "🎬"
                try:
                    await status_msg.edit_text(
                        f"{icon} **{phase_name}...**\n"
                        f"⏳ **Processing: {percent}%**"
                    )
                    last_update_time = now
                except Exception: pass

        # --- PHASE 1 (Always runs) ---
        semaphore = asyncio.Semaphore(5)
        phase_text = "Analyzing Lines" if analyze_only else "Phase 1/2 (Measuring)"
        p1_tasks = [measure_base_chunk(c, user_id, base_rate_str, semaphore, lambda: update_progress(phase_text)) for c in chunks_data]
        p1_results_list = await asyncio.gather(*p1_tasks)
        base_results = {res["index"]: res for res in p1_results_list if res is not None}

        # CALCULATE MATH & FIND BOTTLENECKS
        max_ratio_needed = 1.0
        bottleneck_lines = []

        for chunk in chunks_data:
            idx = chunk["index"]
            res = base_results.get(idx)
            if not res or res["len"] == 0: continue
            base_len = res["len"]
            gap = chunk["gap"]

            if base_len > gap:
                shrink_factor = base_speed_factor / max_speed_factor
                min_possible_len = base_len * shrink_factor
                if min_possible_len > gap:
                    ratio = min_possible_len / gap
                    bottleneck_lines.append(str(idx + 1)) 
                    if ratio > max_ratio_needed:
                        max_ratio_needed = ratio

        # Clean math output
        if target_speed == "auto":
            clean_speed = math.floor((1.0 / max_ratio_needed) * 10) / 10.0
            if clean_speed < 0.6: clean_speed = 0.6
            global_stretch_ratio = 1.0 / clean_speed
            final_speed_reported = f"{clean_speed}x (Auto)"
        else:
            global_stretch_ratio = 1.0 / float(target_speed)
            final_speed_reported = f"{target_speed}x (Manual)"

        # Set friendly global voice name for caption
        friendly_voice_name = next((k for k, v in VOICE_LIB.items() if v == global_voice), global_voice)

        # --- ANALYZE ONLY RETURN ---
        if analyze_only:
            clean_temp(user_id) 
            report = f"📊 **ANALYSIS REPORT**\n\n"
            report += f"⚡ **Calculated Video Speed:** `{final_speed_reported}`\n"
            if bottleneck_lines:
                b_str = ", ".join(bottleneck_lines[:15])
                if len(bottleneck_lines) > 15: b_str += "..."
                report += f"⚠️ **Bottleneck Lines (Require Stretch):**\n`{b_str}`\n\n"
                report += f"💡 *Tip: Shorten these specific lines in your SRT, then Analyze again to get closer to 1.0x speed!*"
            else:
                report += f"✅ **Perfect Fit!** All lines fit naturally inside the gaps. No timeline stretching is needed."
            return True, None, report, friendly_voice_name

        # --- PHASE 2 (Only for Dubbing) ---
        p2_tasks = []
        for chunk in chunks_data:
            res = base_results.get(chunk["index"])
            p2_tasks.append(process_final_chunk(chunk, res, global_stretch_ratio, user_id, base_rate_int, max_fit, semaphore, lambda: update_progress("Phase 2/2 (Finalizing)")))
            
        final_stretched_chunks = await asyncio.gather(*p2_tasks)

        last_sub_end_ms = int(subs[-1].end.ordinal * global_stretch_ratio)
        await asyncio.to_thread(compose_final_audio, final_stretched_chunks, last_sub_end_ms, output_path)
        
        clean_temp(user_id)
        
        # Build final caption
        caption = f"✅ **Dubbed successfully!**\n🗣️ Global Voice: {friendly_voice_name}\n⚡ **Speed Applied: {final_speed_reported}**"
        if bottleneck_lines and target_speed == "auto":
            b_str = ", ".join(bottleneck_lines[:8])
            if len(bottleneck_lines) > 8: b_str += "..."
            caption += f"\n⚠️ Stretch Caused By: {b_str}"
            
        return True, None, caption, friendly_voice_name

    except Exception as e:
        clean_temp(user_id)
        return False, str(e), "Error", "Error"

# --- 🤖 HANDLERS ---
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 Home / Settings"),
        BotCommand("clearall", "🧹 Clear Data")
    ])

def get_settings_keyboard(state):
    v_name = next((k for k, v in VOICE_LIB.items() if v == state['dub_voice']), "Voice")
    s_name = next((k for k, v in SPEED_LIB.items() if v == state['video_speed']), f"{state['video_speed']}x")
    r_name = next((k for k, v in RATE_LIB.items() if v == state['base_rate']), state['base_rate'])
    f_name = f"+{state['max_fit']}%"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗣️ Voice: {v_name}", callback_data="cmd_voices")],
        [InlineKeyboardButton(f"🎙️ Rate: {r_name}", callback_data="cmd_rate"), InlineKeyboardButton(f"🗜️ Fit: {f_name}", callback_data="cmd_fit")],
        [InlineKeyboardButton(f"⏱️ Video Speed: {s_name}", callback_data="cmd_speed")]
    ])

def get_action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Analyze Only", callback_data="trigger_analyze")],
        [InlineKeyboardButton("🎬 Generate Dub Audio", callback_data="trigger_dub")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    await update.message.reply_text(
        "⚙️ **STUDIO SETTINGS**\nConfigure your AI narrator and speed limits below.\n\n"
        "*(Send me an `.srt` file or paste SRT text when you're ready!)*", 
        reply_markup=get_settings_keyboard(state)
    )

async def handle_menu(update, context, lib, prefix, text_msg, columns=2):
    keyboard = []
    row = []
    for name, val in lib.items():
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}_{val}"))
        if len(row) == columns:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="cmd_back")])
    msg = update.message if update.message else update.callback_query.message
    await msg.edit_text(text_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def voices_command(u, c): await handle_menu(u, c, VOICE_LIB, "set_voice", "🗣️ **Select Narrator Voice:**", 2)
async def rate_command(u, c): await handle_menu(u, c, RATE_LIB, "set_rate", "🎙️ **Select Base TTS Speak Rate:**", 3)
async def fit_command(u, c): await handle_menu(u, c, FIT_LIB, "set_fit", "🗜️ **Select Max Emergency Squeeze Speed:**", 3)
async def speed_command(u, c): await handle_menu(u, c, SPEED_LIB, "set_speed", "⏱️ **Select Video Speed Mode:**", 2)

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wipe_user_data(update.effective_user.id)
    await update.message.reply_text("🧹 **All temporary files cleared.**")

async def perform_action(update, context, analyze_only):
    user_id = update.effective_user.id
    p = get_paths(user_id)
    state = get_user_state(user_id)
    msg = update.callback_query.message

    if not os.path.exists(p['srt']):
        await msg.reply_text("❌ **No SRT found. Please send an .srt file first.**")
        return

    icon = "🔍 Analyzing" if analyze_only else "🎬 Dubbing"
    status_msg = await msg.reply_text(f"{icon} Initializing...\n⏳ **Processing: 0%**")
    
    success, error, result_text, voice_name = await process_engine(user_id, p['srt'], p['dub_audio'], state, status_msg, analyze_only)
    
    if success:
        if analyze_only:
            await status_msg.edit_text(result_text, reply_markup=get_action_keyboard())
        else:
            await status_msg.delete()
            # 🚀 BUG FIX: Correctly used msg.chat.id here!
            await context.bot.send_audio(chat_id=msg.chat.id, audio=open(p['dub_audio'], "rb"), caption=result_text)
    else:
        await status_msg.edit_text(f"❌ Failed: {error}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = get_user_state(user_id)
    data = query.data
    
    if data == "cmd_back":
        await query.message.edit_text("⚙️ **STUDIO SETTINGS**", reply_markup=get_settings_keyboard(state))
    elif data == "cmd_voices": await voices_command(update, context)
    elif data == "cmd_speed": await speed_command(update, context)
    elif data == "cmd_rate": await rate_command(update, context)
    elif data == "cmd_fit": await fit_command(update, context)
    elif data == "trigger_analyze": await perform_action(update, context, analyze_only=True)
    elif data == "trigger_dub": await perform_action(update, context, analyze_only=False)
    
    elif data.startswith("set_"):
        if data.startswith("set_voice_"): state['dub_voice'] = data.replace("set_voice_", "")
        elif data.startswith("set_rate_"): state['base_rate'] = data.replace("set_rate_", "")
        elif data.startswith("set_fit_"): state['max_fit'] = int(data.replace("set_fit_", ""))
        elif data.startswith("set_speed_"): 
            val = data.replace("set_speed_", "")
            state['video_speed'] = "auto" if val == "auto" else float(val)
        await query.message.edit_text("⚙️ **STUDIO SETTINGS**", reply_markup=get_settings_keyboard(state))

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
                reply_text = f"✅ **SRT Text Validated!**\n📊 **Total:** {len(subs)} Blocks\n⏱️ **End Time:** {time_str}\n\n*(Paste more or choose an action)*"
            else:
                reply_text = "⚠️ Text saved, but no blocks detected."
        except Exception as e:
            reply_text = f"⚠️ Parsing failed: {e}"

        await msg.reply_text(reply_text, reply_markup=get_action_keyboard())
    else:
        await msg.reply_text("ℹ️ Please send an `.srt` file or paste valid SRT text.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    p = get_paths(user_id)
    if msg.document.file_name.lower().endswith('.srt'):
        await (await msg.document.get_file()).download_to_drive(p['srt'])
        await msg.reply_text("✅ **SRT File Loaded.** Ready for action!", reply_markup=get_action_keyboard())
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

# 🚀 BUG FIX: Cleaned up run_polling to modern PTB v20+ standard
async def main():
    bot_app = ApplicationBuilder().token(TG_TOKEN).post_init(post_init).build()
    
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("clearall", clearall_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    
    # ChatGPT ဖျက်ခိုင်းခဲ့တဲ့ မှန်ကန်တဲ့ PTB v20+ Custom Loop အပိုင်းကို ပြန်ထည့်ခြင်း
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    # Web Server ကို နောက်ကွယ်မှာ ပြိုင်တူ Run ခြင်း
    asyncio.create_task(run_server())
    
    # Bot ကို မပိတ်သွားအောင် ဆက်ဖွင့်ထားပေးခြင်း
    await asyncio.Event().wait()

if __name__ == '__main__': 
    asyncio.run(main())
