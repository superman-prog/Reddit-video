#!/usr/bin/env python3
import os, sys, json, asyncio, subprocess, random, logging
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

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
STORAGE_CHANNEL  = os.getenv("STORAGE_CHANNEL", "YOUR_ID_HERE")
VOICE            = "en-GB-RyanNeural"
VIDEO_W, VIDEO_H = 1080, 1920
WORK_DIR         = Path("./work")
CLIPS_INDEX      = Path("./clips.json")
WORK_DIR.mkdir(exist_ok=True)

WAIT_TITLE, WAIT_STORY, SELECT_MODE, WAIT_IMAGE, WAIT_CONFIRM = range(5)

HELP_TEXT = """
🎬 *Reddit Video Maker Bot* — Help

*Commands:*
/new — Start creating a new video
/addclip — Add a background clip (send video or YouTube URL after)
/clips — See how many background clips are saved
/cancel — Cancel current operation
/help — Show this message

*How to make a video:*
1. /new → Enter a title
2. Enter the story text
3. Choose style: 📸 Reddit (with screenshot) or 🎤 Rant (no image)
4. If Reddit: send a screenshot image
5. Confirm → bot generates and sends the video

*Adding background clips:*
• /addclip then send a video file
• /addclip then paste a YouTube URL
• Or just paste a YouTube URL anytime (no command needed)

*Notes:*
• Videos are 1080×1920 (portrait/Reels format)
• TTS voice: British English (Ryan)
• Background clips are stored persistently
"""

# ── CLIPS STORAGE ─────────────────────────────────────────────────────────────
# Persists to clips.json. On Render, use a mounted disk or Postgres for true
# persistence — clips.json will reset on redeploy without a persistent disk.

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
    if not clips:
        return None
    return random.choice(clips)["file_id"]

# ── HELPERS ──────────────────────────────────────────────────────────────────
def download_yt(url, out):
    opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': str(out),
        'quiet': True,
        'merge_output_format': 'mp4',
    }
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

# ── VIDEO LOGIC ──────────────────────────────────────────────────────────────
async def generate_tts(text, out):
    await edge_tts.Communicate(text, VOICE).save(str(out))

def compose(img, t_audio, s_audio, g_path, story, out):
    t_dur = get_dur(t_audio) if t_audio else 0
    s_dur = get_dur(s_audio)
    words = story.split()
    if not words:
        return False
    per_w = s_dur / len(words)

    caps = []
    for i, w in enumerate(words):
        start = (i * per_w) + t_dur
        end   = ((i + 1) * per_w) + t_dur
        txt   = w.replace("'", "\\'").replace(":", "\\:")
        caps.append(
            f"drawtext=text='{txt}':x=(w-text_w)/2:y=h*0.75"
            f":enable='between(t,{start:.3f},{end:.3f})'"
            f":fontsize=70:fontcolor=white:borderw=4:bordercolor=black"
        )

    v_filt = ",".join(caps)

    if img:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(t_dur), "-i", str(img),
            "-i", str(g_path),
            "-i", str(t_audio),
            "-i", str(s_audio),
            "-filter_complex",
            f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black[v0];"
            f"[1:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,{v_filt}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[outv];"
            f"[2:a][3:a]concat=n=2:v=0:a=1[outa]",
            "-map", "[outv]", "-map", "[outa]",
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(g_path),
            "-i", str(s_audio),
            "-filter_complex",
            f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,{v_filt}[outv]",
            "-map", "[outv]", "-map", "1:a",
        ]

    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", str(out)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        logger.error(f"ffmpeg error: {result.stderr.decode()}")
    return result.returncode == 0

# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "👋 Welcome to *Reddit Video Maker*!\n\nUse /new to create a video or /help for all commands.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_clips(u: Update, c: ContextTypes.DEFAULT_TYPE):
    clips = load_clips()
    await u.message.reply_text(f"🎞 {len(clips)} background clip(s) saved.")

async def cmd_cancel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data.clear()
    await u.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def cmd_new(u: Update, c: ContextTypes.DEFAULT_TYPE):
    clips = load_clips()
    if not clips:
        await u.message.reply_text(
            "⚠️ No background clips saved yet!\n"
            "Use /addclip to add one first, or paste a YouTube URL."
        )
        return ConversationHandler.END
    await u.message.reply_text("🎬 Enter the *title* for your video:", parse_mode=ParseMode.MARKDOWN)
    return WAIT_TITLE

# ── CONVERSATION HANDLERS ─────────────────────────────────────────────────────
async def got_title(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["t"] = u.message.text
    await u.message.reply_text("📖 Now send the *story* text:", parse_mode=ParseMode.MARKDOWN)
    return WAIT_STORY

async def got_story(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["s"] = u.message.text
    kb = [[
        InlineKeyboardButton("📸 Reddit (with screenshot)", callback_data="r"),
        InlineKeyboardButton("🎤 Rant (no image)", callback_data="rt")
    ]]
    await u.message.reply_text("Choose a style:", reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_MODE

async def mode_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "rt":
        c.user_data["img"] = None
        return await finalize(q, c)
    await q.edit_message_text("📸 Send your Reddit screenshot:")
    return WAIT_IMAGE

async def got_img(u: Update, c: ContextTypes.DEFAULT_TYPE):
    file_obj = u.message.photo[-1] if u.message.photo else u.message.document
    c.user_data["img"] = file_obj.file_id
    return await finalize(u, c)

async def finalize(orig, c: ContextTypes.DEFAULT_TYPE):
    title_preview = c.user_data["t"][:40] + ("..." if len(c.user_data["t"]) > 40 else "")
    style = "Reddit 📸" if c.user_data.get("img") else "Rant 🎤"
    txt = f"*Confirm Video*\n\n📝 {title_preview}\n🎨 Style: {style}\n\nProceed?"
    kb = [[
        InlineKeyboardButton("✅ Generate", callback_data="y"),
        InlineKeyboardButton("❌ Cancel", callback_data="n")
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

    msg = await q.edit_message_text("⚙️ Generating your video, please wait...")

    uid  = u.effective_user.id
    bot  = c.bot
    img_p = WORK_DIR / f"{uid}_i.jpg" if c.user_data.get("img") else None
    t_a   = WORK_DIR / f"{uid}_t.mp3" if img_p else None
    s_a   = WORK_DIR / f"{uid}_s.mp3"
    g_p   = WORK_DIR / f"{uid}_g.mp4"
    out   = WORK_DIR / f"{uid}_f.mp4"

    try:
        clip_id = get_random_clip()
        if not clip_id:
            await msg.edit_text("⚠️ No background clips found. Add one with /addclip first.")
            return ConversationHandler.END

        await msg.edit_text("⚙️ Step 1/4: Downloading background clip...")
        clip_file = await bot.get_file(clip_id)
        await clip_file.download_to_drive(str(g_p))

        await msg.edit_text("⚙️ Step 2/4: Generating TTS audio...")
        if img_p:
            img_file = await bot.get_file(c.user_data["img"])
            await img_file.download_to_drive(str(img_p))
            await generate_tts(c.user_data["t"], t_a)
        await generate_tts(c.user_data["s"], s_a)

        await msg.edit_text("⚙️ Step 3/4: Composing video...")
        success = await asyncio.get_event_loop().run_in_executor(
            None, compose, img_p, t_a, s_a, g_p, c.user_data["s"], out
        )

        if not success:
            await msg.edit_text("❌ Video composition failed. Check logs.")
            return ConversationHandler.END

        await msg.edit_text("⚙️ Step 4/4: Uploading...")
        with open(out, "rb") as v:
            await bot.send_video(u.effective_chat.id, v, caption="✅ Your video is ready!")
            v.seek(0)
            await bot.send_video(
                STORAGE_CHANNEL, v,
                caption=f"📦 Backup: {c.user_data['t']}"
            )
        await msg.delete()

    except Exception as e:
        logger.error(f"conf_cb error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        for f in [img_p, t_a, s_a, g_p, out]:
            if f and Path(f).exists():
                Path(f).unlink()

    return ConversationHandler.END

# ── ADDCLIP & YT URL HANDLER ──────────────────────────────────────────────────
async def cmd_addclip(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["ac"] = True
    await u.message.reply_text(
        "📎 Send a video file *or* paste a YouTube URL:",
        parse_mode=ParseMode.MARKDOWN
    )

async def recv_vid(u: Update, c: ContextTypes.DEFAULT_TYPE):
    txt = u.message.text or ""

    # ── YouTube URL (works with or without /addclip) ──────────────────────────
    if "youtu" in txt:
        m = await u.message.reply_text("⏳ Downloading from YouTube...")
        tmp = WORK_DIR / f"yt_{u.effective_user.id}.mp4"
        try:
            # Run blocking download in executor so bot doesn't freeze
            await asyncio.get_event_loop().run_in_executor(None, download_yt, txt, tmp)
            await m.edit_text("📤 Uploading clip to storage...")
            with open(tmp, "rb") as f:
                fwd = await c.bot.send_video(STORAGE_CHANNEL, f)
            add_clip(fwd.video.file_id)
            await m.edit_text(f"✅ YouTube clip added! Total clips: {len(load_clips())}")
        except Exception as e:
            logger.error(f"YT download error: {e}", exc_info=True)
            await m.edit_text(f"❌ Failed to download: {e}")
        finally:
            if tmp.exists():
                tmp.unlink()
        c.user_data.pop("ac", None)
        return

    # ── Video/document sent after /addclip ────────────────────────────────────
    if c.user_data.get("ac") and (u.message.video or u.message.document):
        m = await u.message.reply_text("📤 Forwarding to storage...")
        try:
            fwd = await u.message.forward(STORAGE_CHANNEL)
            vid = fwd.video or (fwd.document if fwd.document and "video" in (fwd.document.mime_type or "") else None)
            if vid:
                add_clip(vid.file_id)
                await m.edit_text(f"✅ Clip added! Total clips: {len(load_clips())}")
            else:
                await m.edit_text("⚠️ Couldn't extract video from that message.")
        except Exception as e:
            logger.error(f"addclip error: {e}", exc_info=True)
            await m.edit_text(f"❌ Error: {e}")
        finally:
            c.user_data.pop("ac", None)

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
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO | filters.TEXT & ~filters.COMMAND,
        recv_vid
    ))

    # ── Render keep-alive web server ──────────────────────────────────────────
    from flask import Flask
    from threading import Thread

    web_app = Flask("")

    @web_app.route("/")
    def home():
        clips = load_clips()
        return f"✅ Bot is alive | 🎞 Clips: {len(clips)}"

    Thread(target=lambda: web_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()

    logger.info("✅ Bot is polling on Render...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
