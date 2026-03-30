#!/usr/bin/env python3
import os, json, asyncio, subprocess, random, logging
import yt_dlp
from pathlib import Path
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telegram.constants import ParseMode
import edge_tts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
STORAGE_CHANNEL = os.getenv("STORAGE_CHANNEL", "YOUR_ID_HERE")
VOICE           = "en-GB-RyanNeural"
VIDEO_W         = 720
VIDEO_H         = 1280
WORK_DIR        = Path("./work")
CLIPS_INDEX     = Path("./clips.json")
COOKIES_FILE    = Path(os.getenv("COOKIES_PATH", "./cookies.txt"))   # export from browser, place in repo root
WORK_DIR.mkdir(exist_ok=True)

WAIT_TITLE, WAIT_STORY, SELECT_MODE, WAIT_IMAGE, WAIT_CONFIRM = range(5)

HELP_TEXT = """
🎬 *Reddit Video Maker Bot* — Help

*Commands:*
/new — Start creating a new video
/addclip — Add a background clip \(send video or YouTube URL after\)
/clips — See how many background clips are saved
/cancel — Cancel current operation
/help — Show this message

*How to make a video:*
1\. /new → Enter a title
2\. Enter the story text
3\. Choose style: 📸 Reddit \(with screenshot\) or 🎤 Rant \(no image\)
4\. If Reddit: send a screenshot image
5\. Confirm → bot generates and sends the video

*Adding background clips:*
• /addclip then send a video file
• /addclip then paste a YouTube URL
• Or just paste a YouTube URL anytime \(no command needed\)

*Notes:*
• Output: 720×1280 portrait \(Reels/Shorts format\)
• TTS voice: British English \(Ryan\)
• Captions are word\-grouped for readability & low RAM usage
"""

# ── CLIPS STORAGE ─────────────────────────────────────────────────────────────
def load_clips():
    try:
        if CLIPS_INDEX.exists():
            return json.loads(CLIPS_INDEX.read_text())
    except Exception as e:
        logger.error(f"load_clips error: {e}")
    return []

def save_clips(clips):
    try:
        CLIPS_INDEX.write_text(json.dumps(clips, indent=2))
    except Exception as e:
        logger.error(f"save_clips error: {e}")

def add_clip(file_id):
    clips = load_clips()
    if not any(c["file_id"] == file_id for c in clips):
        clips.append({"file_id": file_id, "added": datetime.now().isoformat()})
        save_clips(clips)
        logger.info(f"Clip added: {file_id}")

def get_random_clip():
    clips = load_clips()
    return random.choice(clips)["file_id"] if clips else None

# ── HELPERS ───────────────────────────────────────────────────────────────────
def download_yt(url, out):
    opts = {
        # 'best' acts as a fallback if the combined 'bestvideo+bestaudio' fails
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(out),
        "quiet": True,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
        
def get_dur(path):
    r = subprocess.run(
        ["ffprobe", "-v", "0", "-show_entries", "format=duration",
         "-of", "compact=p=0:nk=1", str(path)],
        capture_output=True, text=True
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 5.0

# ── SUBTITLE GENERATION ───────────────────────────────────────────────────────
def _ass_time(s: float) -> str:
    h  = int(s // 3600)
    m  = int((s % 3600) // 60)
    se = s % 60
    return f"{h}:{m:02d}:{se:05.2f}"

def build_ass(story: str, t_dur: float, s_dur: float,
              ass_path: Path, words_per_line: int = 4) -> bool:
    """
    Write an ASS subtitle file grouping words into lines of N words.
    Single subtitle filter is O(1) complexity vs O(N) per-word drawtext —
    safe for 3-min videos on 512 MB Render free tier.
    """
    words = story.split()
    if not words:
        return False

    per_word = s_dur / len(words)
    margin_v = int(VIDEO_H * 0.18)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_W}\n"
        f"PlayResY: {VIDEO_H}\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,Arial,62,&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,10,10,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    events = []
    for i in range(0, len(words), words_per_line):
        chunk = words[i : i + words_per_line]
        start = t_dur + i * per_word
        end   = t_dur + (i + len(chunk)) * per_word
        text  = (
            " ".join(chunk)
            .replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
        )
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
            f"Default,,0,0,0,,{text}"
        )

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return True

# ── VIDEO COMPOSITION ─────────────────────────────────────────────────────────
async def generate_tts(text: str, out: Path):
    await edge_tts.Communicate(text, VOICE).save(str(out))

def compose(img, t_audio, s_audio, g_path, story, out, ass_path) -> bool:
    t_dur = get_dur(t_audio) if t_audio else 0
    s_dur = get_dur(s_audio)

    if not build_ass(story, t_dur, s_dur, ass_path):
        return False

    scale_pad = (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    sub = f"ass={ass_path}"

    if img:
        fc = (
            f"[0:v]{scale_pad}[v0];"
            f"[1:v]{scale_pad},{sub}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[outv];"
            f"[2:a][3:a]concat=n=2:v=0:a=1[outa]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(t_dur), "-i", str(img),
            "-i", str(g_path),
            "-i", str(t_audio),
            "-i", str(s_audio),
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "[outa]",
        ]
    else:
        fc = f"[0:v]{scale_pad},{sub}[outv]"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(g_path),
            "-i", str(s_audio),
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "1:a",
        ]

    cmd += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-threads", "1",    # caps RAM usage on Render free tier
        str(out),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        logger.error(f"ffmpeg stderr:\n{result.stderr.decode()}")
    return result.returncode == 0

# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "👋 Welcome to *Reddit Video Maker*!\n\nUse /new to create a video or /help for all commands.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_clips(u: Update, c: ContextTypes.DEFAULT_TYPE):
    n = len(load_clips())
    await u.message.reply_text(f"🎞 {n} background clip(s) saved.")

async def cmd_cancel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data.clear()
    await u.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def cmd_new(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not load_clips():
        await u.message.reply_text(
            "⚠️ No background clips yet!\n"
            "Paste a YouTube URL or use /addclip to add one first."
        )
        return ConversationHandler.END
    await u.message.reply_text("🎬 Enter the *title* for your video:", parse_mode=ParseMode.MARKDOWN)
    return WAIT_TITLE

# ── CONVERSATION STATES ───────────────────────────────────────────────────────
async def got_title(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["t"] = u.message.text
    await u.message.reply_text("📖 Now send the *story* text:", parse_mode=ParseMode.MARKDOWN)
    return WAIT_STORY

async def got_story(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["s"] = u.message.text
    kb = [[
        InlineKeyboardButton("📸 Reddit (screenshot)", callback_data="r"),
        InlineKeyboardButton("🎤 Rant (no image)",     callback_data="rt"),
    ]]
    await u.message.reply_text("Choose a style:", reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_MODE

async def mode_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "rt":
        c.user_data["img"] = None
        return await _show_confirm(q, c)
    await q.edit_message_text("📸 Send your Reddit screenshot:")
    return WAIT_IMAGE

async def got_img(u: Update, c: ContextTypes.DEFAULT_TYPE):
    obj = u.message.photo[-1] if u.message.photo else u.message.document
    c.user_data["img"] = obj.file_id
    return await _show_confirm(u, c)

async def _show_confirm(orig, c: ContextTypes.DEFAULT_TYPE):
    preview = c.user_data["t"][:40] + ("…" if len(c.user_data["t"]) > 40 else "")
    style   = "Reddit 📸" if c.user_data.get("img") else "Rant 🎤"
    txt     = f"*Confirm Video*\n\n📝 {preview}\n🎨 Style: {style}\n\nProceed?"
    kb      = [[
        InlineKeyboardButton("✅ Generate", callback_data="y"),
        InlineKeyboardButton("❌ Cancel",   callback_data="n"),
    ]]
    if hasattr(orig, "edit_message_text"):
        await orig.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await orig.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return WAIT_CONFIRM

async def conf_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "n":
        await q.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    msg = await q.edit_message_text("⚙️ Starting generation…")
    uid = u.effective_user.id
    bot = c.bot

    img_p = WORK_DIR / f"{uid}_i.jpg" if c.user_data.get("img") else None
    t_a   = WORK_DIR / f"{uid}_t.mp3" if img_p else None
    s_a   = WORK_DIR / f"{uid}_s.mp3"
    g_p   = WORK_DIR / f"{uid}_g.mp4"
    out   = WORK_DIR / f"{uid}_f.mp4"
    ass_p = WORK_DIR / f"{uid}_subs.ass"

    try:
        clip_id = get_random_clip()
        if not clip_id:
            await msg.edit_text("⚠️ No background clips. Add one with /addclip.")
            return ConversationHandler.END

        await msg.edit_text("⚙️ [1/4] Downloading background clip…")
        await (await bot.get_file(clip_id)).download_to_drive(str(g_p))

        await msg.edit_text("⚙️ [2/4] Generating TTS audio…")
        if img_p:
            await (await bot.get_file(c.user_data["img"])).download_to_drive(str(img_p))
            await generate_tts(c.user_data["t"], t_a)
        await generate_tts(c.user_data["s"], s_a)

        await msg.edit_text("⚙️ [3/4] Composing video (may take a few minutes)…")
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None, compose, img_p, t_a, s_a, g_p, c.user_data["s"], out, ass_p
        )
        if not ok:
            await msg.edit_text("❌ ffmpeg failed — check Render logs.")
            return ConversationHandler.END

        await msg.edit_text("⚙️ [4/4] Uploading…")
        with open(out, "rb") as v:
            await bot.send_video(u.effective_chat.id, v, caption="✅ Done!")
            v.seek(0)
            await bot.send_video(STORAGE_CHANNEL, v, caption=f"📦 {c.user_data['t']}")
        await msg.delete()

    except Exception as e:
        logger.error("conf_cb error", exc_info=True)
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        for f in [img_p, t_a, s_a, g_p, out, ass_p]:
            if f and Path(f).exists():
                Path(f).unlink()

    return ConversationHandler.END

async def cmd_cookiecheck(u: Update, c: ContextTypes.DEFAULT_TYPE):
    lines = []
    lines.append(f"CWD: {Path('.').resolve()}")
    lines.append(f"COOKIES_FILE path: {COOKIES_FILE.resolve()}")
    lines.append(f"COOKIES_FILE exists: {COOKIES_FILE.exists()}")
    if COOKIES_FILE.exists():
        lines.append(f"Size: {COOKIES_FILE.stat().st_size} bytes")
        lines.append("First 3 lines:")
        with open(COOKIES_FILE) as f:
            for i, line in enumerate(f):
                if i >= 3: break
                lines.append(f"  {line.rstrip()}")
    else:
        # List files in cwd to help debug
        files = [str(p) for p in Path(".").iterdir()]
        lines.append("Files in CWD: " + ", ".join(files[:20]))
    await u.message.reply_text("\n".join(lines))

# ── ADDCLIP + BARE YT URL ─────────────────────────────────────────────────────
async def cmd_addclip(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["ac"] = True
    await u.message.reply_text(
        "📎 Send a video file *or* paste a YouTube URL:",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_formats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    parts = u.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await u.message.reply_text("Usage: /formats <youtube_url>")
        return
    url = parts[1].strip()
    m = await u.message.reply_text("Fetching available formats...")
    opts = {"quiet": True, "skip_download": True}
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    try:
        def _list():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                lines = ["format_id  ext    resolution   vcodec     acodec", "-"*55]
                for f in info.get("formats", []):
                    lines.append(
                        f"{f.get('format_id','?'):10} "
                        f"{f.get('ext','?'):6} "
                        f"{str(f.get('resolution','?')):12} "
                        f"{str(f.get('vcodec','?'))[:10]:10} "
                        f"{str(f.get('acodec','?'))[:10]}"
                    )
                return "\n".join(lines)
        result = await asyncio.get_event_loop().run_in_executor(None, _list)
        for chunk in [result[i:i+3900] for i in range(0, len(result), 3900)]:
            await u.message.reply_text(f"```\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN)
        await m.delete()
    except Exception as e:
        await m.edit_text(f"Error: {e}")

async def recv_vid(u: Update, c: ContextTypes.DEFAULT_TYPE):
    txt = u.message.text or ""

    # YouTube URL — works even without /addclip
    if "youtu" in txt:
        m   = await u.message.reply_text("⏳ Downloading from YouTube…")
        tmp = WORK_DIR / f"yt_{u.effective_user.id}.mp4"
        try:
            await asyncio.get_event_loop().run_in_executor(None, download_yt, txt, tmp)
            await m.edit_text("📤 Uploading to storage channel…")
            with open(tmp, "rb") as f:
                fwd = await c.bot.send_video(STORAGE_CHANNEL, f)
            add_clip(fwd.video.file_id)
            await m.edit_text(f"✅ Clip added! Total: {len(load_clips())}")
        except Exception as e:
            logger.error("YT download error", exc_info=True)
            await m.edit_text(f"❌ Failed: {e}")
        finally:
            if tmp.exists():
                tmp.unlink()
        c.user_data.pop("ac", None)
        return

    # Video file sent after /addclip
    if c.user_data.get("ac") and (u.message.video or u.message.document):
        m = await u.message.reply_text("📤 Forwarding to storage…")
        try:
            fwd = await u.message.forward(STORAGE_CHANNEL)
            vid = fwd.video or (
                fwd.document
                if fwd.document and "video" in (fwd.document.mime_type or "")
                else None
            )
            if vid:
                add_clip(vid.file_id)
                await m.edit_text(f"✅ Clip added! Total: {len(load_clips())}")
            else:
                await m.edit_text("⚠️ Couldn't find a video in that message.")
        except Exception as e:
            logger.error("addclip fwd error", exc_info=True)
            await m.edit_text(f"❌ Error: {e}")
        finally:
            c.user_data.pop("ac", None)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            WAIT_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            WAIT_STORY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_story)],
            SELECT_MODE:  [CallbackQueryHandler(mode_cb)],
            WAIT_IMAGE:   [MessageHandler(filters.PHOTO | filters.Document.IMAGE, got_img)],
            WAIT_CONFIRM: [CallbackQueryHandler(conf_cb, pattern="^[yn]$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("clips",   cmd_clips))
    app.add_handler(CommandHandler("addclip", cmd_addclip))
    app.add_handler(CommandHandler("formats", cmd_formats))
    app.add_handler(CommandHandler("cookiecheck", cmd_cookiecheck))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO | (filters.TEXT & ~filters.COMMAND),
        recv_vid,
    ))

    # Render keep-alive web server
    from flask import Flask
    from threading import Thread

    flask_app = Flask("")

    @flask_app.route("/")
    def home():
        return f"Bot alive | Clips: {len(load_clips())}"

    Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))),
        daemon=True,
    ).start()

    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
