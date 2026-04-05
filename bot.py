import asyncio
import functools
import os
import re
import logging
import shutil
import subprocess
import tempfile
import time
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Allowed Telegram user IDs — only these users can use the bot.
ALLOWED_USERS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"
)

DEFAULT_QUALITY = "720"

# Per-user quality preference (in-memory, resets on restart)
user_quality = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Hey! Send me a YouTube link and I'll download it, "
        "upload it to Google Drive, and send you the public link.\n\n"
        "Commands:\n"
        "/quality — choose download quality (video or MP3 audio)\n"
        "/clip START END LINK — download a specific portion\n"
        f"Current default: {DEFAULT_QUALITY}p"
    )


QUALITY_OPTIONS = ["360", "480", "720", "1080", "1440", "2160", "mp3"]


async def set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    current = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
    buttons = _build_quality_buttons(current)
    await update.message.reply_text(
        "Select download quality:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _build_quality_buttons(current):
    buttons = []
    for q in QUALITY_OPTIONS:
        display = "MP3 (audio only)" if q == "mp3" else f"{q}p"
        marker = "> " if q == current else ""
        suffix = "  (current)" if q == current else ""
        buttons.append([InlineKeyboardButton(f"{marker}{display}{suffix}", callback_data=f"quality_{q}")])
    return buttons


async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.data.startswith("quality_"):
        return

    if update.effective_user.id not in ALLOWED_USERS:
        await query.answer("Not authorized.")
        return

    quality = query.data.replace("quality_", "")
    user_quality[update.effective_user.id] = quality
    display = "MP3 (audio only)" if quality == "mp3" else f"{quality}p"
    await query.answer(f"Quality set to {display}")

    buttons = _build_quality_buttons(quality)
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


def _format_duration(seconds):
    if not seconds:
        return "Unknown"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _progress_bar(percent):
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {percent}%"


async def _poll_video_ready(update: Update, file_id: str, file_name: str):
    """Poll Google Drive until the video is processed and watchable, then notify."""
    from drive_service import check_video_ready
    try:
        for _ in range(60):  # Check for up to 30 minutes (60 × 30s)
            await asyncio.sleep(30)
            ready = await asyncio.to_thread(check_video_ready, file_id)
            if ready:
                await update.message.reply_text(f"✅ Your video is now watchable on Drive:\n{file_name}")
                return
        # If still not ready after 30 min, let the user know
        await update.message.reply_text(f"⏳ Video still processing after 30 min:\n{file_name}")
    except Exception as e:
        logger.error(f"Error polling video status: {e}", exc_info=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    text = update.message.text or ""
    match = YOUTUBE_REGEX.search(text)

    if not match:
        await update.message.reply_text("Please send me a valid YouTube link.")
        return

    url = match.group()
    if not url.startswith("http"):
        url = "https://" + url

    status_msg = await update.message.reply_text("Fetching video info...")

    tmp_dir = tempfile.mkdtemp()
    try:
        # Extract info first (no download) to get title, duration
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown")
        duration = _format_duration(info.get("duration"))
        video_id = info.get("id", "")
        thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

        await update.message.reply_text(title)
        await update.message.reply_text(duration)
        await update.message.reply_text(thumb_url)

        # Now download with chosen quality
        quality = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
        loop = asyncio.get_event_loop()
        last_update = [0]
        finished_streams = [0]
        is_two_stream = quality != "mp3"

        def download_hook(d):
            if d["status"] == "finished":
                finished_streams[0] += 1
                return
            if d["status"] == "downloading":
                pct_str = d.get("_percent_str", "0%").strip().replace("%", "")
                try:
                    stream_pct = float(pct_str)
                except ValueError:
                    return
                if is_two_stream:
                    if finished_streams[0] == 0:
                        percent = int(stream_pct * 0.8)
                    else:
                        percent = 80 + int(stream_pct * 0.2)
                else:
                    percent = int(stream_pct)
                percent = min(percent, 99)
                now = time.time()
                if now - last_update[0] >= 2:
                    last_update[0] = now
                    label = "Downloading audio (MP3)" if quality == "mp3" else f"Downloading ({quality}p)"
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(f"{label}\n{_progress_bar(percent)}"),
                        loop,
                    )

        if quality == "mp3":
            await status_msg.edit_text(f"Downloading audio (MP3)\n{_progress_bar(0)}")
            ydl_opts = {
                "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [download_hook],
            }
        else:
            await status_msg.edit_text(f"Downloading ({quality}p)\n{_progress_bar(0)}")
            ydl_opts = {
                "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [download_hook],
            }

        def do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                dl_info = ydl.extract_info(url, download=True)
                fname = ydl.prepare_filename(dl_info)
                if quality == "mp3":
                    fname = os.path.splitext(fname)[0] + ".mp3"
                return fname

        file_name = await asyncio.to_thread(do_download)

        label = "Downloading audio (MP3)" if quality == "mp3" else f"Downloading ({quality}p)"
        await status_msg.edit_text(f"{label}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)
        await status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(0)}")

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 2:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=file_name,
            file_name=os.path.basename(file_name),
            progress_callback=upload_progress,
        )
        await status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(100)}")

        # Delete the status message and send the link as a new message
        await status_msg.delete()
        await update.message.reply_text(drive_link)

        # Check if it's a video and start monitoring processing
        ext = os.path.splitext(file_name)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
            asyncio.create_task(_poll_video_ready(update, file_id, os.path.basename(file_name)))

    except Exception as e:
        logger.error(f"Error processing {url}: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"Something went wrong:\n{e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    # Get file info for naming and size
    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name or "file"
        file_size = file_obj.file_size or 0
    elif update.message.video:
        file_obj = update.message.video
        file_name = file_obj.file_name or "video.mp4"
        file_size = file_obj.file_size or 0
    elif update.message.audio:
        file_obj = update.message.audio
        file_name = file_obj.file_name or "audio.mp3"
        file_size = file_obj.file_size or 0
    elif update.message.voice:
        file_obj = update.message.voice
        file_name = "voice.ogg"
        file_size = file_obj.file_size or 0
    elif update.message.video_note:
        file_obj = update.message.video_note
        file_name = "video_note.mp4"
        file_size = file_obj.file_size or 0
    elif update.message.photo:
        file_obj = update.message.photo[-1]
        file_name = "photo.jpg"
        file_size = file_obj.file_size or 0
    else:
        return

    status_msg = await update.message.reply_text(f"Downloading {file_name}...")
    tmp_dir = tempfile.mkdtemp()
    try:
        local_path = os.path.join(tmp_dir, file_name)
        loop = asyncio.get_event_loop()

        if file_size > 20 * 1024 * 1024:
            size_mb = file_size / (1024 * 1024)
            await status_msg.edit_text(
                f"File too large ({size_mb:.1f} MB). Telegram Bot API limit is 20 MB.\n"
                "For larger files, use YouTube links instead."
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(local_path)

        await status_msg.edit_text(f"Downloading {file_name}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 2:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        await status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(0)}")

        from drive_service import upload_to_drive

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=local_path,
            file_name=file_name,
            progress_callback=upload_progress,
        )

        await status_msg.delete()
        await update.message.reply_text(drive_link)

        # Check if it's a video and start monitoring processing
        ext = os.path.splitext(file_name)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".mp4"):
            asyncio.create_task(_poll_video_ready(update, file_id, file_name))

    except Exception as e:
        logger.error(f"Error uploading file: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"Something went wrong:\n{e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _parse_timestamp(ts):
    """Parse timestamp like '1:35' or '1:02:30' into seconds."""
    parts = ts.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


async def clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /clip START END LINK\n"
            "Example: /clip 1:35 4:24 https://youtu.be/abc123"
        )
        return

    start_ts, end_ts = args[0], args[1]
    url = args[2]

    try:
        start_sec = _parse_timestamp(start_ts)
        end_sec = _parse_timestamp(end_ts)
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid timestamp format. Use MM:SS or H:MM:SS.")
        return

    if end_sec <= start_sec:
        await update.message.reply_text("End time must be after start time.")
        return

    if not url.startswith("http"):
        url = "https://" + url

    match = YOUTUBE_REGEX.search(url)
    if not match:
        await update.message.reply_text("Please provide a valid YouTube link.")
        return

    status_msg = await update.message.reply_text("Fetching video info...")
    tmp_dir = tempfile.mkdtemp()
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown")
        video_id = info.get("id", "")

        await update.message.reply_text(f"{title}\nClip: {start_ts} → {end_ts}")

        quality = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
        loop = asyncio.get_event_loop()
        last_update = [0]
        finished_streams = [0]
        is_two_stream = quality != "mp3"

        def download_hook(d):
            if d["status"] == "finished":
                finished_streams[0] += 1
                return
            if d["status"] == "downloading":
                pct_str = d.get("_percent_str", "0%").strip().replace("%", "")
                try:
                    stream_pct = float(pct_str)
                except ValueError:
                    return
                if is_two_stream:
                    if finished_streams[0] == 0:
                        percent = int(stream_pct * 0.6)
                    else:
                        percent = 60 + int(stream_pct * 0.15)
                else:
                    percent = int(stream_pct * 0.75)
                percent = min(percent, 75)
                now = time.time()
                if now - last_update[0] >= 2:
                    last_update[0] = now
                    label = "Downloading clip (MP3)" if quality == "mp3" else f"Downloading clip ({quality}p)"
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(f"{label}\n{_progress_bar(percent)}"),
                        loop,
                    )

        if quality == "mp3":
            await status_msg.edit_text(f"Downloading clip (MP3)\n{_progress_bar(0)}")
            ydl_opts = {
                "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [download_hook],
            }
        else:
            await status_msg.edit_text(f"Downloading clip ({quality}p)\n{_progress_bar(0)}")
            ydl_opts = {
                "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [download_hook],
            }

        def do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                dl_info = ydl.extract_info(url, download=True)
                fname = ydl.prepare_filename(dl_info)
                if quality == "mp3":
                    fname = os.path.splitext(fname)[0] + ".mp3"
                    # For mp3, yt-dlp may not have the postprocessor yet
                    # We'll handle audio extraction in ffmpeg cut step
                return fname

        file_name = await asyncio.to_thread(do_download)

        # Now cut the clip with ffmpeg
        label = "Downloading clip (MP3)" if quality == "mp3" else f"Downloading clip ({quality}p)"
        await status_msg.edit_text(f"Cutting clip with ffmpeg...\n{_progress_bar(80)}")

        duration = end_sec - start_sec
        if quality == "mp3":
            # Extract audio clip as mp3
            clip_path = os.path.join(tmp_dir, f"clip_{start_ts.replace(':','-')}_{end_ts.replace(':','-')}.mp3")
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", file_name,
                "-t", str(duration),
                "-vn",
                "-acodec", "libmp3lame",
                "-ab", "192k",
                clip_path,
            ]
        else:
            clip_path = os.path.join(tmp_dir, f"clip_{start_ts.replace(':','-')}_{end_ts.replace(':','-')}.mp4")
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", file_name,
                "-t", str(duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                clip_path,
            ]

        await asyncio.to_thread(
            subprocess.run, ffmpeg_cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        await status_msg.edit_text(f"{label}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)
        await status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(0)}")

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 2:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive

        # Build a nice clip file name from the title
        safe_title = re.sub(r'[<>:"/\\|?*]', '', title)[:80]
        ext = ".mp3" if quality == "mp3" else ".mp4"
        clip_name = f"{safe_title} [{start_ts}-{end_ts}]{ext}"

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=clip_path,
            file_name=clip_name,
            progress_callback=upload_progress,
        )
        await status_msg.edit_text(f"Uploading to Google Drive\n{_progress_bar(100)}")

        await status_msg.delete()
        await update.message.reply_text(drive_link)

        # Monitor video processing (not for mp3)
        if quality != "mp3":
            asyncio.create_task(_poll_video_ready(update, file_id, clip_name))

    except Exception as e:
        logger.error(f"Error processing clip {url}: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"Something went wrong:\n{e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def post_init(application):
    """Set the bot's command menu button."""
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("quality", "Choose download quality"),
        BotCommand("clip", "Download a clip: /clip START END LINK"),
    ])


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quality", set_quality))
    app.add_handler(CommandHandler("clip", clip))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^quality_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.PHOTO,
        handle_file,
    ))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
