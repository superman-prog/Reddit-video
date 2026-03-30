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

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
STORAGE_CHANNEL = os.getenv("STORAGE_CHANNEL", "YOUR_ID_HERE")
VOICE          = "en-GB-RyanNeural"
VIDEO_W, VIDEO_H = 1080, 1920
WORK_DIR, CLIPS_INDEX = Path("./work"), Path("./clips.json")
WORK_DIR.mkdir(exist_ok=True)

WAIT_TITLE, WAIT_STORY, SELECT_MODE, WAIT_IMAGE, WAIT_CONFIRM = range(5)

# ── HELPERS ──────────────────────────────────────────────────────────────────
def load_clips():
    return json.loads(CLIPS_INDEX.read_text()) if CLIPS_INDEX.exists() else []

def save_clips(clips):
    CLIPS_INDEX.write_text(json.dumps(clips, indent=2))

def add_clip(file_id):
    clips = load_clips()
    if not any(c["file_id"] == file_id for c in clips):
        clips.append({"file_id": file_id, "added": datetime.now().isoformat()})
        save_clips(clips)

def download_yt(url, out):
    opts = {'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best','outtmpl': str(out),'quiet': True}
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])

def get_dur(path):
    r = subprocess.run(["ffprobe","-v","0","-show_entries","format=duration","-of","compact=p=0:nk=1",str(path)], capture_output=True, text=True)
    return float(r.stdout.strip() or 5.0)

# ── VIDEO LOGIC ──────────────────────────────────────────────────────────────
async def generate_tts(text, out):
    await edge_tts.Communicate(text, VOICE).save(str(out))

def compose(img, t_audio, s_audio, g_path, story, out):
    t_dur = get_dur(t_audio) if t_audio else 0
    s_dur = get_dur(s_audio)
    words = story.split()
    per_w = s_dur / len(words)
    
    caps = []
    for i, w in enumerate(words):
        start, end = (i*per_w)+t_dur, ((i+1)*per_w)+t_dur
        txt = w.replace("'","\\'").replace(":","\\:")
        caps.append(f"drawtext=text='{txt}':x=(w-text_w)/2:y=h*0.75:enable='between(t,{start:.3f},{end:.3f})':fontsize=70:fontcolor=white:borderw=4:bordercolor=black")
    
    v_filt = ",".join(caps)
    if img:
        cmd = ["ffmpeg","-y","-loop","1","-t",str(t_dur),"-i",str(img),"-i",str(g_path),"-i",str(t_audio),"-i",str(s_audio),"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black[v0];[1:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,{v_filt}[v1];[v0][v1]concat=n=2:v=1:a=0[outv];[2:a][3:a]concat=n=2:v=0:a=1[outa]","-map","[outv]","-map","[outa]"]
    else:
        cmd = ["ffmpeg","-y","-i",str(g_path),"-i",str(s_audio),"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,{v_filt}[outv]","-map","[outv]","-map","1:a"]
    
    cmd += ["-c:v","libx264","-preset","ultrafast","-crf","28",str(out)]
    return subprocess.run(cmd).returncode == 0

# ── HANDLERS ──────────────────────────────────────────────────────────────────
async def cmd_new(u, c):
    await u.message.reply_text("🎬 Title:")
    return WAIT_TITLE

async def got_title(u, c):
    c.user_data["t"] = u.message.text
    await u.message.reply_text("📖 Story:")
    return WAIT_STORY

async def got_story(u, c):
    c.user_data["s"] = u.message.text
    kb = [[InlineKeyboardButton("📸 Reddit", callback_data="r"), InlineKeyboardButton("🎤 Rant", callback_data="rt")]]
    await u.message.reply_text("Style?", reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_MODE

async def mode_cb(u, c):
    q = u.callback_query
    await q.answer()
    if q.data == "rt":
        c.user_data["img"] = None
        return await finalize(q, c)
    await q.edit_message_text("📸 Send Screenshot:")
    return WAIT_IMAGE

async def got_img(u, c):
    c.user_data["img"] = (u.message.photo[-1] if u.message.photo else u.message.document).file_id
    return await finalize(u, c)

async def finalize(orig, c):
    kb = [[InlineKeyboardButton("✅ Go", callback_data="y"), InlineKeyboardButton("❌ No", callback_data="n")]]
    txt = f"Confirm:\n{c.user_data['t'][:30]}..."
    if hasattr(orig, "edit_message_text"): await orig.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else: await orig.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    return WAIT_CONFIRM

async def conf_cb(u, c):
    q = u.callback_query
    await q.answer()
    if q.data == "n": return ConversationHandler.END
    msg = await q.edit_message_text("⚙️ Generating...")
    
    uid, bot = u.effective_user.id, c.bot
    img_p = WORK_DIR/f"{uid}_i.jpg" if c.user_data["img"] else None
    t_a = WORK_DIR/f"{uid}_t.mp3" if img_p else None
    s_a, g_p, out = WORK_DIR/f"{uid}_s.mp3", WORK_DIR/f"{uid}_g.mp4", WORK_DIR/f"{uid}_f.mp4"
    
    try:
        if img_p:
            await (await bot.get_file(c.user_data["img"])).download_to_drive(str(img_p))
            await generate_tts(c.user_data["t"], t_a)
        await generate_tts(c.user_data["s"], s_a)
        await (await bot.get_file(random.choice(load_clips())["file_id"])).download_to_drive(str(g_p))
        
        if compose(img_p, t_a, s_a, g_p, c.user_data["s"], out):
            with open(out, "rb") as v:
                await bot.send_video(u.effective_chat.id, v, caption="✅ Done")
                v.seek(0)
                await bot.send_video(STORAGE_CHANNEL, v, caption=f"📦 Backup: {c.user_data['t']}")
            await msg.delete()
    finally:
        for f in [img_p, t_a, s_a, g_p, out]:
            if f and f.exists(): f.unlink()
    return ConversationHandler.END

async def recv_vid(u, c):
    txt = u.message.text or ""
    if "youtu" in txt:
        m = await u.message.reply_text("⚡ YT Downloader...")
        tmp = WORK_DIR/f"yt_{u.effective_user.id}.mp4"
        download_yt(txt, tmp)
        with open(tmp, 'rb') as f:
            fwd = await c.bot.send_video(STORAGE_CHANNEL, f)
            add_clip(fwd.video.file_id)
        await m.edit_text("✅ Added!")
        tmp.unlink()
    elif c.user_data.get("ac"):
        fwd = await u.message.forward(STORAGE_CHANNEL)
        add_clip(fwd.video.file_id)
        await u.message.reply_text("✅ Added!")
        c.user_data.pop("ac")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            WAIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            WAIT_STORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_story)],
            SELECT_MODE: [CallbackQueryHandler(mode_cb)],
            WAIT_IMAGE: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, got_image)],
            WAIT_CONFIRM: [CallbackQueryHandler(conf_cb, pattern="^[yn]$")],
        }, fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    ))
    app.add_handler(CommandHandler("addclip", lambda u,c: (c.user_data.update({"ac":True}), u.message.reply_text("Link or Video:"))))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.TEXT, recv_vid))

    # --- RENDER KEEP-ALIVE START ---
    from flask import Flask
    from threading import Thread
    import logging

    web_app = Flask('')
    @web_app.route('/')
    def home(): return "Bot is Awake"

    def run_web():
        web_app.run(host='0.0.0.0', port=10000)

    print("🌐 Starting Keep-Alive Server...")
    Thread(target=run_web).start()
    # --- RENDER KEEP-ALIVE END ---

    print("🚀 Bot is Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
