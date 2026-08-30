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
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
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

# Matches any URL — yt-dlp supports 1000+ sites
URL_REGEX = re.compile(r"https?://[^\s]+")

DEFAULT_QUALITY = "720"

# Per-user quality preference (in-memory, resets on restart)
user_quality = {}

# Per-user cancellation flag — checked during long-running tasks
cancel_flags = {}

# Per-user download queue
user_queues: dict[int, asyncio.Queue] = {}
user_queue_workers: dict[int, asyncio.Task] = {}

# Rename state: maps user_id -> file_id awaiting new name
pending_renames: dict[int, str] = {}

_last_edit = {}

async def safe_edit(msg, text, **kwargs):
    now = time.time()
    key = msg.message_id
    if now - _last_edit.get(key, 0) < 2:
        return
    try:
        await msg.edit_text(text, **kwargs)
        _last_edit[key] = now
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    except Exception as e:
        if "RetryAfter" in type(e).__name__ or "Flood" in str(e) or "429" in str(e):
            logger.warning(f"Flood limit hit, backing off: {e}")
            _last_edit[key] = now + 30
        else:
            raise


class TaskCancelled(Exception):
    pass


def _check_cancel(user_id):
    """Raise TaskCancelled if the user requested cancellation."""
    if cancel_flags.get(user_id):
        raise TaskCancelled("Task cancelled by user.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _friendly_error(e):
    msg = str(e)
    if "Bounced" in msg:
        return "This tweet/post has been removed or is no longer available."
    if "Video unavailable" in msg or "video is unavailable" in msg.lower():
        return "This video is unavailable. It may have been removed or is private."
    if "Private video" in msg:
        return "This is a private video. You need access to view it."
    if "Sign in to confirm" in msg or "age" in msg.lower() and "confirm" in msg.lower():
        return "This video requires age verification and can't be downloaded."
    if "copyright" in msg.lower():
        return "This video was removed due to a copyright claim."
    if "not available" in msg.lower() and "country" in msg.lower():
        return "This video is not available in your region."
    if "Requested format is not available" in msg:
        return "The selected quality isn't available for this video. Try a different quality."
    if "Incomplete data" in msg or "giving up" in msg.lower():
        return "Download failed — YouTube interrupted the connection. Try again."
    if "HTTP Error 403" in msg:
        return "🚫 YouTube is temporarily blocking downloads from this IP. Too many requests were made. Usually clears in a few hours. Use /ytcheck to monitor."
    if "HTTP Error 429" in msg:
        return "Too many requests — YouTube is rate-limiting. Wait a minute and try again."
    if "No video formats found" in msg or "Unsupported URL" in msg:
        return "This link isn't supported or doesn't contain a downloadable video."
    return None


def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS


def _setup_auth_callback(update, loop):
    """Set up the Google Drive auth callback to send the sign-in link via the bot."""
    import drive_service
    def on_auth_url(url):
        asyncio.run_coroutine_threadsafe(
            update.message.reply_text(
                f"Google sign-in required.\n\n"
                f"1. Open this link:\n{url}\n\n"
                f"2. Sign in with your Google account and allow access\n"
                f"3. You'll be redirected to a page that won't load — that's normal\n"
                f"4. Copy the FULL URL from your browser's address bar and send it here"
            ),
            loop,
        )
    drive_service.auth_url_callback = on_auth_url


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Hey! Send me a YouTube link and I'll download it, "
        "upload it to Google Drive, and send you the public link.\n\n"
        "You can also send me any file (up to 2GB) and I'll upload it to Drive.\n\n"
        "Commands:\n\n"
        "/quality — choose download quality (360p–4K or MP3)\n\n"
        "/clip — download a specific portion of a video\n"
        "  Usage: /clip START END LINK\n"
        "  Example: /clip 1:35 4:24 https://youtu.be/abc123\n"
        "  Times can be M:SS or H:MM:SS\n\n"
        "/combine — combine multiple clips into one video\n"
        "  Usage: send /combine followed by one clip per line:\n"
        "  /combine\n"
        "  1:35 4:24 https://youtu.be/abc123\n"
        "  0:00 2:30 https://youtu.be/def456\n"
        "  3:10 5:00 https://youtu.be/abc123\n"
        "  (same link can appear more than once)\n\n"
        "/cancel — stop the current download or upload\n"
        "/logout — sign out of Google Drive\n"
        "/retry FOLDER — re-upload a failed file\n\n"
        f"Current quality: {DEFAULT_QUALITY}p"
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


def _format_size(bytes_val):
    if not bytes_val:
        return "Unknown"
    b = int(bytes_val)
    if b < 1024**2:
        return f"{b / 1024:.0f} KB"
    if b < 1024**3:
        return f"{b / (1024**2):.1f} MB"
    return f"{b / (1024**3):.2f} GB"


def _progress_bar(percent):
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {percent}%"


def _add_music_metadata(file_path, info, force=False):
    """Add ID3 tags + cover art to MP3 if the video is categorized as Music."""
    categories = info.get("categories") or []
    if not force and "Music" not in categories:
        return
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, TRCK, TDRC, TCOM, APIC, USLT
        from mutagen.mp3 import MP3
    except ImportError:
        return
    try:
        audio = MP3(file_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        title = info.get("title", "")
        artist = info.get("artist") or info.get("uploader", "")
        album = info.get("_playlist_album") or info.get("album") or title
        genre = info.get("genre", "")
        track = info.get("_playlist_track") or info.get("track_number") or 1
        track_total = info.get("_playlist_total")
        year = info.get("release_year") or (info.get("upload_date", "")[:4] if info.get("upload_date") else "")
        composer = info.get("composer") or artist
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=[artist]))
        tags.add(TPE2(encoding=3, text=[artist]))
        tags.add(TALB(encoding=3, text=[album]))
        if genre:
            tags.add(TCON(encoding=3, text=[genre]))
        track_str = f"{track}/{track_total}" if track_total else str(track)
        tags.add(TRCK(encoding=3, text=[track_str]))
        if year:
            tags.add(TDRC(encoding=3, text=[str(year)]))
        tags.add(TCOM(encoding=3, text=[composer]))
        # Lyrics — try to get from subtitles
        import urllib.request, io
        try:
            subs = info.get("subtitles") or {}
            auto_subs = info.get("automatic_captions") or {}
            vid_lang = info.get("language") or ""
            sub_data = None
            sub_lang = "und"
            if vid_lang and vid_lang in subs:
                sub_data = subs[vid_lang]
                sub_lang = vid_lang
            if not sub_data and subs:
                sub_lang = next(iter(subs))
                sub_data = subs[sub_lang]
            if not sub_data and vid_lang and vid_lang in auto_subs:
                sub_data = auto_subs[vid_lang]
                sub_lang = vid_lang
            if not sub_data and auto_subs:
                sub_lang = next(iter(auto_subs))
                sub_data = auto_subs[sub_lang]
            if sub_data:
                sub_url = None
                for fmt in sub_data:
                    if fmt.get("ext") in ("vtt", "srv1", "json3"):
                        sub_url = fmt.get("url")
                        break
                if not sub_url and sub_data:
                    sub_url = sub_data[0].get("url")
                if sub_url:
                    raw = urllib.request.urlopen(sub_url).read().decode("utf-8", errors="replace")
                    lines = []
                    for line in raw.split("\n"):
                        line = line.strip()
                        if not line or line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
                            continue
                        if "-->" in line or line.isdigit():
                            continue
                        clean = re.sub(r"<[^>]+>", "", line)
                        if clean and (not lines or clean != lines[-1]):
                            lines.append(clean)
                    if lines:
                        lyrics_text = "\n".join(lines)
                        lang3 = (sub_lang[:3] if len(sub_lang) >= 3 else sub_lang.ljust(3))[:3]
                        tags.add(USLT(encoding=3, lang=lang3, desc="", text=lyrics_text))
        except Exception:
            pass
        # Cover art — try Topic channel thumbnail (square album art) first
        import urllib.request, io
        thumb_url = None
        try:
            search_q = f"ytsearch1:{artist} {title} topic"
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
                sr = ydl.extract_info(search_q, download=False)
                entries = sr.get("entries") or []
                if entries:
                    ch = entries[0].get("channel", "") or entries[0].get("uploader", "")
                    if "topic" in ch.lower() or "Topic" in ch:
                        thumb_url = entries[0].get("thumbnail", "")
        except Exception:
            pass
        if not thumb_url:
            thumb_url = info.get("thumbnail", "")
        if thumb_url:
            thumb_data = urllib.request.urlopen(thumb_url).read()
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(thumb_data))
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                w, h = img.size
                if w != h:
                    s = min(w, h)
                    left = (w - s) // 2
                    top = (h - s) // 2
                    img = img.crop((left, top, left + s, top + s))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                thumb_data = buf.getvalue()
            except ImportError:
                pass
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=thumb_data))
        audio.save()
    except Exception:
        pass


def _ensure_drive_friendly(file_path):
    """Re-encode video to H.264/AAC MP4 if it's not already — helps Drive process faster."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
        return file_path
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", file_path],
            capture_output=True, text=True, timeout=30
        )
        vcodec = probe.stdout.strip()
        probe_a = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", file_path],
            capture_output=True, text=True, timeout=30
        )
        acodec = probe_a.stdout.strip()
        if vcodec == "h264" and acodec in ("aac", ""):
            out_path = file_path + ".tmp.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", file_path,
                 "-c", "copy",
                 "-movflags", "+faststart",
                 "-brand", "isom",
                 out_path],
                check=True, capture_output=True, timeout=300
            )
            os.remove(file_path)
            final = os.path.splitext(file_path)[0] + ".mp4"
            os.rename(out_path, final)
            return final
        out_path = file_path + ".tmp.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path,
             "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
             "-pix_fmt", "yuv420p",
             "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             "-brand", "isom",
             out_path],
            check=True, capture_output=True, timeout=600
        )
        os.remove(file_path)
        final = os.path.splitext(file_path)[0] + ".mp4"
        os.rename(out_path, final)
        return final
    except Exception as e:
        logger.warning(f"Could not re-encode {file_path}: {e}")
        return file_path


async def _poll_video_ready(update: Update, file_id: str, file_name: str, link_msg=None):
    from drive_service import check_video_ready
    reply_to = link_msg.message_id if link_msg else None
    try:
        for _ in range(120):
            await asyncio.sleep(30)
            ready = await asyncio.to_thread(check_video_ready, file_id)
            if ready:
                await update.message.reply_text(f"✅ Your video is now watchable on Drive:\n{file_name}", reply_to_message_id=reply_to)
                return
        for _ in range(12):
            await asyncio.sleep(300)
            ready = await asyncio.to_thread(check_video_ready, file_id)
            if ready:
                await update.message.reply_text(f"✅ Your video is now watchable on Drive:\n{file_name}", reply_to_message_id=reply_to)
                return
        await update.message.reply_text(f"⏳ Video still processing after 2 hours:\n{file_name}", reply_to_message_id=reply_to)
    except Exception as e:
        logger.error(f"Error polling video status: {e}", exc_info=True)


async def _process_url(url, update, status_msg):
    tmp_dir = tempfile.mkdtemp(dir="/home/ms/tmp")
    uid = update.effective_user.id
    cancel_flags[uid] = False
    try:
        await safe_edit(status_msg, "Fetching video info...")
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown")
        duration = _format_duration(info.get("duration"))
        video_id = info.get("id", "")
        thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

        await update.message.reply_text(title)
        await update.message.reply_text(duration)
        await update.message.reply_text(thumb_url)

        quality = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
        loop = asyncio.get_event_loop()
        last_update = [0]
        finished_streams = [0]
        is_two_stream = quality != "mp3"

        def download_hook(d):
            _check_cancel(uid)
            if d["status"] == "finished":
                finished_streams[0] += 1
                return
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if not total:
                    return
                stream_pct = (downloaded / total) * 100
                if is_two_stream:
                    if finished_streams[0] == 0:
                        percent = int(stream_pct * 0.8)
                    else:
                        percent = 80 + int(stream_pct * 0.2)
                else:
                    percent = int(stream_pct)
                percent = min(percent, 99)
                now = time.time()
                if now - last_update[0] >= 1:
                    last_update[0] = now
                    label = "Downloading audio (MP3)" if quality == "mp3" else f"Downloading ({quality}p)"
                    size_info = f"\n{_format_size(downloaded)} / {_format_size(total)}" if total else ""
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(status_msg,f"{label}\n{_progress_bar(percent)}{size_info}"),
                        loop,
                    )

        if quality == "mp3":
            await safe_edit(status_msg,f"Downloading audio (MP3)\n{_progress_bar(0)}")
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
            await safe_edit(status_msg,f"Downloading ({quality}p)\n{_progress_bar(0)}")
            vid_w = info.get("width") or 0
            vid_h = info.get("height") or 0
            if vid_h > vid_w and vid_w > 0:
                dim = f"width<={quality}"
            else:
                dim = f"height<={quality}"
            ydl_opts = {
                "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                "format": f"bestvideo[{dim}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[{dim}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[{dim}]+bestaudio/best[height<={quality}]/best",
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
        _check_cancel(uid)

        if quality == "mp3":
            await asyncio.to_thread(_add_music_metadata, file_name, info)
        else:
            await safe_edit(status_msg,"Preparing for upload...")
            file_name = await asyncio.to_thread(_ensure_drive_friendly, file_name)
            _check_cancel(uid)

        label = "Downloading audio (MP3)" if quality == "mp3" else f"Downloading ({quality}p)"
        await safe_edit(status_msg,f"{label}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)
        file_size_str = _format_size(os.path.getsize(file_name))
        await safe_edit(status_msg,f"Uploading to Google Drive ({file_size_str})\n{_progress_bar(0)}")

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 1:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,f"Uploading to Google Drive ({file_size_str})\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive
        _setup_auth_callback(update, loop)

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=file_name,
            file_name=os.path.basename(file_name),
            progress_callback=upload_progress,
        )
        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(100)}")

        await status_msg.delete()
        link_msg = await update.message.reply_text(drive_link, reply_to_message_id=update.message.message_id)

        ext = os.path.splitext(file_name)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
            asyncio.create_task(_poll_video_ready(update, file_id, os.path.basename(file_name), link_msg))

        shutil.rmtree(tmp_dir, ignore_errors=True)

    except TaskCancelled:
        await safe_edit(status_msg,"⏹ Task cancelled.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Error processing {url}: {e}", exc_info=True)
        try:
            await safe_edit(status_msg,
                f"{_friendly_error(e) or f'Something went wrong: {e}'}\n\nUse /files to retry upload or delete."
            )
        except Exception:
            pass


async def _queue_worker(uid, app):
    q = user_queues[uid]
    while True:
        url, update, status_msg = await q.get()
        try:
            await _process_url(url, update, status_msg)
        except Exception as e:
            logger.error(f"Queue worker error: {e}", exc_info=True)
        finally:
            q.task_done()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    uid = update.effective_user.id
    text = update.message.text or ""

    # Handle pending rename
    if uid in pending_renames and text and not URL_REGEX.search(text):
        file_id = pending_renames.pop(uid)
        try:
            from drive_service import rename_file
            new_name = await asyncio.to_thread(rename_file, file_id, text.strip())
            await update.message.reply_text(f"✅ Renamed to: {new_name}")
        except Exception as e:
            await update.message.reply_text(f"Rename failed: {e}")
        return

    # Check if user is sending back Google auth redirect URL
    import drive_service
    if "code=" in text and ("localhost" in text or "127.0.0.1" in text):
        drive_service.set_auth_response(text.strip())
        msg = await update.message.reply_text("Got it! Signing in...")
        token_path = os.path.join(os.path.dirname(__file__), "token.json")
        for _ in range(30):
            await asyncio.sleep(1)
            if os.path.exists(token_path):
                break
        try:
            from drive_service import _get_service
            service = await asyncio.to_thread(_get_service)
            about = await asyncio.to_thread(
                lambda: service.about().get(fields="user").execute()
            )
            email = about.get("user", {}).get("emailAddress", "unknown")
            await msg.edit_text(f"✅ Signed in to {email}")
        except Exception as e:
            await msg.edit_text(f"Signed in, but couldn't fetch email:\n{e}")
        return

    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Please send me a valid link (YouTube, Twitter/X, TikTok, Instagram, etc.).")
        return

    url = match.group()
    if not url.startswith("http"):
        url = "https://" + url
    url = url.replace('youtub.com/', 'youtube.com/')

    # Enqueue the URL for sequential processing
    if uid not in user_queues:
        user_queues[uid] = asyncio.Queue()
    q = user_queues[uid]

    position = q.qsize()
    if position > 0:
        status_msg = await update.message.reply_text(f"📋 Queued (position {position + 1}). Will start when current download finishes.")
    else:
        status_msg = await update.message.reply_text("Fetching video info...")

    q.put_nowait((url, update, status_msg))

    if uid not in user_queue_workers or user_queue_workers[uid].done():
        user_queue_workers[uid] = asyncio.create_task(_queue_worker(uid, context.application))


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
    tmp_dir = tempfile.mkdtemp(dir="/home/ms/tmp")
    try:
        local_path = os.path.join(tmp_dir, file_name)
        loop = asyncio.get_event_loop()

        if file_size > 4000 * 1024 * 1024:
            size_mb = file_size / (1024 * 1024)
            await safe_edit(status_msg,
                f"File too large ({size_mb:.1f} MB). Limit is 2000 MB."
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        tg_file = await file_obj.get_file()
        fp = tg_file.file_path or ""
        # Local Bot API returns URL like http://...//home/ms/... — extract the local path
        idx = fp.find("//home/")
        if idx >= 0:
            fp = fp[idx + 1:]  # /home/ms/telegram-bot-api-data/.../file.mp4
        logger.info(f"Resolved file path: {fp}")
        if os.path.exists(fp):
            shutil.copy2(fp, local_path)
        else:
            await tg_file.download_to_drive(local_path)

        await safe_edit(status_msg,f"Downloading {file_name}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)

        ext = os.path.splitext(file_name)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
            await safe_edit(status_msg, "Preparing video for Drive...")
            local_path = await asyncio.to_thread(_ensure_drive_friendly, local_path)
            file_name = os.path.basename(local_path)

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 1:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(0)}")

        from drive_service import upload_to_drive
        _setup_auth_callback(update, loop)

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=local_path,
            file_name=file_name,
            progress_callback=upload_progress,
        )

        await status_msg.delete()
        link_msg = await update.message.reply_text(drive_link, reply_to_message_id=update.message.message_id)

        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
            asyncio.create_task(_poll_video_ready(update, file_id, file_name, link_msg))

        shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"Error uploading file: {e}", exc_info=True)
        try:
            await safe_edit(status_msg,
                f"{_friendly_error(e) or f'Something went wrong: {e}'}\n\nUse /files to retry upload or delete."
            )
        except Exception:
            pass


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

    match = URL_REGEX.search(url)
    if not match:
        await update.message.reply_text("Please provide a valid link.")
        return
    url = match.group()

    status_msg = await update.message.reply_text("Fetching video info...")
    tmp_dir = tempfile.mkdtemp(dir="/home/ms/tmp")
    uid = update.effective_user.id
    cancel_flags[uid] = False
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown")
        video_id = info.get("id", "")
        is_live = info.get("is_live", False)

        await update.message.reply_text(f"{title}\nClip: {start_ts} → {end_ts}")

        quality = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
        loop = asyncio.get_event_loop()
        last_update = [0]
        finished_streams = [0]
        is_two_stream = quality != "mp3"

        def download_hook(d):
            _check_cancel(uid)
            if d["status"] == "finished":
                finished_streams[0] += 1
                return
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if not total:
                    return
                stream_pct = (downloaded / total) * 100
                if is_two_stream:
                    if finished_streams[0] == 0:
                        percent = int(stream_pct * 0.6)
                    else:
                        percent = 60 + int(stream_pct * 0.15)
                else:
                    percent = int(stream_pct * 0.75)
                percent = min(percent, 75)
                now = time.time()
                if now - last_update[0] >= 1:
                    last_update[0] = now
                    label = "Downloading clip (MP3)" if quality == "mp3" else f"Downloading clip ({quality}p)"
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(status_msg,f"{label}\n{_progress_bar(percent)}"),
                        loop,
                    )

        duration = end_sec - start_sec
        safe_title = re.sub(r'[<>:"/\\|?*]', '', title)[:80]
        ext = ".mp3" if quality == "mp3" else ".mp4"
        clip_path = os.path.join(tmp_dir, f"clip_{start_ts.replace(':','-')}_{end_ts.replace(':','-')}{ext}")

        label = "Downloading clip (MP3)" if quality == "mp3" else f"Downloading clip ({quality}p)"
        if is_live:
            label = "Downloading live clip (MP3)" if quality == "mp3" else f"Downloading live clip ({quality}p)"
        await safe_edit(status_msg, f"{label}\n{_progress_bar(0)}")

        if is_live:
            # Live stream: use yt-dlp subprocess with --download-sections
            await safe_edit(status_msg, f"{label}\n{_progress_bar(5)}")
            yt_dlp_bin = shutil.which("yt-dlp") or "/home/ms/telegram-yt-drive-bot/venv/bin/yt-dlp"
            section = f"*{start_sec}-{end_sec}"
            if quality == "mp3":
                ytdl_cmd = [yt_dlp_bin, "--live-from-start", "--download-sections", section,
                            "--force-keyframes-at-cuts", "-f", "bestaudio/best",
                            "-x", "--audio-format", "mp3",
                            "-o", os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                            "--no-playlist", "--quiet", url]
            else:
                vid_w = info.get("width") or 0
                vid_h = info.get("height") or 0
                if vid_h > vid_w and vid_w > 0:
                    fmt = f"bestvideo[width<={quality}]+bestaudio/best"
                else:
                    fmt = f"bestvideo[height<={quality}]+bestaudio/best"
                ytdl_cmd = [yt_dlp_bin, "--live-from-start", "--download-sections", section,
                            "--force-keyframes-at-cuts", "-f", fmt, "--merge-output-format", "mp4",
                            "-o", os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                            "--no-playlist", "--quiet", url]

            def run_ytdlp_live():
                proc = subprocess.Popen(ytdl_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                start_time = time.time()
                while proc.poll() is None:
                    if cancel_flags.get(uid):
                        proc.kill()
                        proc.wait()
                        raise TaskCancelled()
                    elapsed = time.time() - start_time
                    pct = min(int((elapsed / max(duration * 2, 1)) * 80) + 10, 90)
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(status_msg, f"{label}\n{_progress_bar(pct)}"), loop)
                    time.sleep(3)
                if proc.returncode != 0:
                    stderr = proc.stderr.read().decode(errors="replace")
                    raise Exception(f"yt-dlp live clip failed: {stderr[:500]}")

            await asyncio.to_thread(run_ytdlp_live)
            _check_cancel(uid)
            files = [f for f in os.listdir(tmp_dir) if f.endswith((".mp4", ".mp3", ".mkv", ".webm"))]
            if not files:
                raise Exception("No output file from live clip download")
            file_name = os.path.join(tmp_dir, files[0])
            shutil.move(file_name, clip_path)
        else:
            # Regular video: try partial download, fallback to full + ffmpeg
            if quality == "mp3":
                ydl_opts = {
                    "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                    "format": "bestaudio/best",
                    "download_ranges": yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
                    "force_keyframes_at_cuts": True,
                    "noplaylist": True,
                    "quiet": True,
                    "progress_hooks": [download_hook],
                }
            else:
                vid_w = info.get("width") or 0
                vid_h = info.get("height") or 0
                if vid_h > vid_w and vid_w > 0:
                    dim = f"width<={quality}"
                else:
                    dim = f"height<={quality}"
                ydl_opts = {
                    "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                    "format": f"bestvideo[{dim}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[{dim}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[{dim}]+bestaudio/best[height<={quality}]/best",
                    "merge_output_format": "mp4",
                    "download_ranges": yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
                    "force_keyframes_at_cuts": True,
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

            try:
                file_name = await asyncio.to_thread(do_download)
                _check_cancel(uid)
                shutil.move(file_name, clip_path)
            except Exception as partial_err:
                if "partially downloaded" not in str(partial_err) and "cannot be" not in str(partial_err):
                    raise
                logger.info("Partial download not supported, falling back to full download + ffmpeg cut")
                await safe_edit(status_msg, f"{label}\n{_progress_bar(0)}")
            for k in ("download_ranges", "force_keyframes_at_cuts", "live_from_start"):
                ydl_opts.pop(k, None)
            finished_streams[0] = 0

            def do_full_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    dl_info = ydl.extract_info(url, download=True)
                    fname = ydl.prepare_filename(dl_info)
                    if quality == "mp3":
                        fname = os.path.splitext(fname)[0] + ".mp3"
                    return fname

            file_name = await asyncio.to_thread(do_full_download)
            _check_cancel(uid)
            await safe_edit(status_msg,"Cutting clip...")
            if quality == "mp3":
                ffmpeg_cmd = ["ffmpeg", "-y", "-ss", str(start_sec), "-i", file_name, "-t", str(duration), "-vn", "-acodec", "libmp3lame", "-ab", "192k", clip_path]
            else:
                ffmpeg_cmd = ["ffmpeg", "-y", "-ss", str(start_sec), "-i", file_name, "-t", str(duration), "-c", "copy", "-avoid_negative_ts", "make_zero", clip_path]
            await asyncio.to_thread(subprocess.run, ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if quality == "mp3":
            await asyncio.to_thread(_add_music_metadata, clip_path, info)

        await safe_edit(status_msg,f"{label}\n{_progress_bar(100)}")
        await asyncio.sleep(0.5)
        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(0)}")

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 1:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive
        _setup_auth_callback(update, loop)

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
        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(100)}")

        await status_msg.delete()
        link_msg = await update.message.reply_text(drive_link, reply_to_message_id=update.message.message_id)

        if quality != "mp3":
            asyncio.create_task(_poll_video_ready(update, file_id, clip_name, link_msg))

        shutil.rmtree(tmp_dir, ignore_errors=True)

    except TaskCancelled:
        await safe_edit(status_msg,"⏹ Task cancelled.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Error processing clip {url}: {e}", exc_info=True)
        try:
            await safe_edit(status_msg,
                f"{_friendly_error(e) or f'Something went wrong: {e}'}\n\nUse /files to retry upload or delete."
            )
        except Exception:
            pass


async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /retry FOLDER_PATH\nPaste the folder path from the error message.")
        return

    folder_path = " ".join(args)
    if not os.path.isdir(folder_path):
        await update.message.reply_text("Folder not found. It may have been cleaned up already.")
        return

    # Find the file to upload (largest file in the folder)
    files = []
    for root, dirs, filenames in os.walk(folder_path):
        for fn in filenames:
            fp = os.path.join(root, fn)
            files.append((fp, os.path.getsize(fp)))

    if not files:
        await update.message.reply_text("No files found in that folder.")
        return

    # Pick the largest file (likely the final video)
    files.sort(key=lambda x: x[1], reverse=True)
    file_path = files[0][0]
    file_name = os.path.basename(file_path)

    status_msg = await update.message.reply_text(f"Re-uploading {file_name} to Google Drive...\n{_progress_bar(0)}")
    try:
        loop = asyncio.get_event_loop()
        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 1:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive
        _setup_auth_callback(update, loop)

        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=file_path,
            file_name=file_name,
            progress_callback=upload_progress,
        )
        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(100)}")
        await status_msg.delete()
        await update.message.reply_text(drive_link)

        # Clean up now that upload succeeded
        shutil.rmtree(folder_path, ignore_errors=True)

        ext = os.path.splitext(file_name)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
            asyncio.create_task(_poll_video_ready(update, file_id, file_name))

    except Exception as e:
        logger.error(f"Error retrying upload: {e}", exc_info=True)
        try:
            await safe_edit(status_msg,f"Retry failed:\n{e}\n\nFiles still kept at:\n{folder_path}")
        except Exception:
            pass


async def combine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    # Parse the message text after /combine
    full_text = update.message.text or ""
    # Remove the /combine command itself
    lines_text = full_text.split("\n", 1)
    if len(lines_text) < 2 or not lines_text[1].strip():
        await update.message.reply_text(
            "Usage: /combine\n"
            "START END LINK\n"
            "START END LINK\n"
            "...\n\n"
            "Example:\n"
            "/combine\n"
            "1:35 4:24 https://youtu.be/abc123\n"
            "0:00 2:30 https://youtu.be/def456\n"
            "3:10 5:00 https://youtu.be/ghi789"
        )
        return

    # Parse each line
    clips = []
    for line in lines_text[1].strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            await update.message.reply_text(f"Invalid line: `{line}`\nEach line needs: START END LINK")
            return
        start_ts, end_ts, url = parts[0], parts[1], parts[2]
        try:
            start_sec = _parse_timestamp(start_ts)
            end_sec = _parse_timestamp(end_ts)
        except (ValueError, IndexError):
            await update.message.reply_text(f"Invalid timestamp in: `{line}`\nUse MM:SS or H:MM:SS format.")
            return
        if end_sec <= start_sec:
            await update.message.reply_text(f"End time must be after start time in: `{line}`")
            return
        if not url.startswith("http"):
            url = "https://" + url
        if not URL_REGEX.search(url):
            await update.message.reply_text(f"Invalid link in: `{line}`")
            return
        clips.append({"start_ts": start_ts, "end_ts": end_ts, "start_sec": start_sec, "end_sec": end_sec, "url": url})

    if len(clips) < 2:
        await update.message.reply_text("Please provide at least 2 clips to combine.")
        return

    status_msg = await update.message.reply_text(f"Processing {len(clips)} clips...")
    tmp_dir = tempfile.mkdtemp(dir="/home/ms/tmp")
    uid = update.effective_user.id
    cancel_flags[uid] = False
    try:
        quality = user_quality.get(update.effective_user.id, DEFAULT_QUALITY)
        loop = asyncio.get_event_loop()
        clip_files = []

        # Deduplicate URLs — download each unique video only once
        unique_urls = list(dict.fromkeys(c["url"] for c in clips))
        downloaded = {}  # url -> file_path
        # Track which clips belong to which URL for immediate cutting
        url_to_clips = {}
        for i, c in enumerate(clips):
            url_to_clips.setdefault(c["url"], []).append((i, c))

        # Pre-allocate clip_files slots
        clip_files = [None] * len(clips)
        cut_tasks = []

        async def cut_clip(idx, source_file, c):
            duration = c["end_sec"] - c["start_sec"]
            normalized_path = os.path.join(tmp_dir, f"norm_{idx}.mp4")
            normalize_cmd = [
                "ffmpeg", "-y",
                "-ss", str(c["start_sec"]),
                "-i", source_file,
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-r", "30",
                "-vf", f"scale=-2:{quality}",
                "-avoid_negative_ts", "make_zero",
                normalized_path,
            ]
            await asyncio.to_thread(
                subprocess.run, normalize_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            clip_files[idx] = normalized_path

        for dl_idx, url in enumerate(unique_urls):
            dl_num = dl_idx + 1
            await safe_edit(status_msg,f"Downloading video {dl_num}/{len(unique_urls)} ({quality}p)\n{_progress_bar(0)}")

            last_update = [0]
            finished_streams = [0]

            def make_hook(dn, total, fin_streams):
                def download_hook(d):
                    _check_cancel(uid)
                    if d["status"] == "finished":
                        fin_streams[0] += 1
                        return
                    if d["status"] == "downloading":
                        downloaded = d.get("downloaded_bytes", 0)
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        if not total:
                            return
                        stream_pct = (downloaded / total) * 100
                        if fin_streams[0] == 0:
                            percent = int(stream_pct * 0.8)
                        else:
                            percent = 80 + int(stream_pct * 0.2)
                        percent = min(percent, 99)
                        now = time.time()
                        if now - last_update[0] >= 1:
                            last_update[0] = now
                            asyncio.run_coroutine_threadsafe(
                                safe_edit(status_msg,
                                    f"Downloading video {dn}/{total} ({quality}p)\n{_progress_bar(percent)}"
                                ),
                                loop,
                            )
                return download_hook

            dl_dir = os.path.join(tmp_dir, f"dl_{dl_idx}")
            os.makedirs(dl_dir, exist_ok=True)

            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
                clip_info = ydl.extract_info(url, download=False)
            cw = clip_info.get("width") or 0
            ch = clip_info.get("height") or 0
            if ch > cw and cw > 0:
                dim = f"width<={quality}"
            else:
                dim = f"height<={quality}"
            ydl_opts = {
                "outtmpl": os.path.join(dl_dir, "%(title)s.%(ext)s"),
                "format": f"bestvideo[{dim}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[{dim}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[{dim}]+bestaudio/best[height<={quality}]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [make_hook(dl_num, len(unique_urls), finished_streams)],
            }

            def do_download(opts, dl_url):
                with yt_dlp.YoutubeDL(opts) as ydl:
                    dl_info = ydl.extract_info(dl_url, download=True)
                    return ydl.prepare_filename(dl_info)

            file_name = await asyncio.to_thread(do_download, ydl_opts, url)
            downloaded[url] = file_name

            # Immediately start cutting all clips from this video in the background
            for idx, c in url_to_clips[url]:
                task = asyncio.create_task(cut_clip(idx, file_name, c))
                cut_tasks.append(task)

        # Wait for all cuts to finish
        await safe_edit(status_msg,f"Cutting {len(clips)} clips...")
        await asyncio.gather(*cut_tasks)

        # Combine all clips using ffmpeg concat
        await safe_edit(status_msg,f"Combining {len(clips)} clips...\n{_progress_bar(50)}")

        # Create concat file list
        concat_list_path = os.path.join(tmp_dir, "concat_list.txt")
        with open(concat_list_path, "w") as f:
            for cf in clip_files:
                f.write(f"file '{cf}'\n")

        combined_path = os.path.join(tmp_dir, "combined.mp4")
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            combined_path,
        ]
        await asyncio.to_thread(
            subprocess.run, concat_cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(0)}")

        last_upload_update = [0]

        def upload_progress(percent):
            now = time.time()
            if now - last_upload_update[0] >= 1:
                last_upload_update[0] = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(percent)}"),
                    loop,
                )

        from drive_service import upload_to_drive
        _setup_auth_callback(update, loop)

        combined_name = f"Combined {len(clips)} clips.mp4"
        file_id, drive_link = await asyncio.to_thread(
            upload_to_drive,
            file_path=combined_path,
            file_name=combined_name,
            progress_callback=upload_progress,
        )
        await safe_edit(status_msg,f"Uploading to Google Drive\n{_progress_bar(100)}")

        await status_msg.delete()
        link_msg = await update.message.reply_text(drive_link, reply_to_message_id=update.message.message_id)

        asyncio.create_task(_poll_video_ready(update, file_id, combined_name, link_msg))

        shutil.rmtree(tmp_dir, ignore_errors=True)

    except TaskCancelled:
        await safe_edit(status_msg,"⏹ Task cancelled.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Error processing combine: {e}", exc_info=True)
        try:
            await safe_edit(status_msg,
                f"{_friendly_error(e) or f'Something went wrong: {e}'}\n\nUse /files to retry upload or delete."
            )
        except Exception:
            pass


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        os.remove(token_path)
        await update.message.reply_text("✅ Logged out. Use /login to sign in again.")
    else:
        await update.message.reply_text("No account is logged in. Use /login to sign in.")


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        await update.message.reply_text("Already logged in. Use /logout first if you want to switch accounts.")
        return
    loop = asyncio.get_event_loop()
    _setup_auth_callback(update, loop)
    import drive_service
    await asyncio.to_thread(drive_service._get_service)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if not os.path.exists(token_path):
        await update.message.reply_text("Not logged in to Google Drive.")
        return
    try:
        import json
        with open(token_path) as f:
            token_data = json.load(f)
        email = token_data.get("client_id", "")
        # Use Drive API to get the actual account email
        from drive_service import _get_service
        service = await asyncio.to_thread(_get_service)
        about = await asyncio.to_thread(
            lambda: service.about().get(fields="user").execute()
        )
        email = about.get("user", {}).get("emailAddress", "unknown")
        await update.message.reply_text(f"✅ Logged in as: {email}")
    except Exception as e:
        await update.message.reply_text(f"Logged in but couldn't fetch account info:\n{e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    cancel_flags[uid] = True
    await update.message.reply_text("⏹ Cancelling current task...")


def _folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    tmp_roots = set()
    tmp_roots.add(tempfile.gettempdir())
    tmp_roots.add("/home/ms/tmp")
    folders = []
    for tmp_root in tmp_roots:
        if not os.path.isdir(tmp_root):
            continue
        try:
            for name in os.listdir(tmp_root):
                full = os.path.join(tmp_root, name)
                if os.path.isdir(full):
                    size = _folder_size(full)
                    if size > 0:
                        folders.append((full, size))
        except Exception:
            pass

    try:
        st = shutil.disk_usage("/")
        used_gb = (st.total - st.free) / (1024 ** 3)
        total_gb = st.total / (1024 ** 3)
        free_gb = st.free / (1024 ** 3)
        pct = int((used_gb / total_gb) * 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        disk_info = f"💾 Disk: {used_gb:.1f} / {total_gb:.1f} GB ({free_gb:.1f} GB free)\n[{bar}] {pct}%"
    except Exception:
        disk_info = ""

    if not folders:
        await update.message.reply_text(f"No temp files.\n\n{disk_info}")
        return

    total_size = sum(s for _, s in folders)
    total_mb = total_size / (1024 * 1024)
    folders.sort(key=lambda x: -x[1])

    # Delete all button
    await update.message.reply_text(
        f"📂 {len(folders)} temp folders ({total_mb:.1f} MB total)\n\n{disk_info}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Delete All", callback_data="tmp_delall"),
        ]]),
    )

    for full, size in folders:
        size_mb = size / (1024 * 1024)
        files_in = []
        for root, _, fs in os.walk(full):
            for f in fs:
                fp = os.path.join(root, f)
                try:
                    fs_size = os.path.getsize(fp)
                    if fs_size >= 1024 * 1024:
                        size_str = f"{fs_size / (1024 * 1024):.1f} MB"
                    else:
                        size_str = f"{fs_size / 1024:.0f} KB"
                    files_in.append(f"  • {f} ({size_str})")
                except OSError:
                    pass
        files_text = "\n".join(files_in[:10])
        if len(files_in) > 10:
            files_text += f"\n  ...and {len(files_in) - 10} more"

        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬆️ Upload", callback_data=f"tmp_upload|{full}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"tmp_del|{full}"),
        ]])
        await update.message.reply_text(
            f"📁 {size_mb:.1f} MB\n{files_text}",
            reply_markup=buttons,
        )


async def tmp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS:
        await query.answer("Not authorized.")
        return
    await query.answer()
    if query.data == "tmp_delall":
        count = 0
        for tmp_root in ["/home/ms/tmp", tempfile.gettempdir()]:
            if not os.path.isdir(tmp_root):
                continue
            for name in os.listdir(tmp_root):
                full = os.path.join(tmp_root, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                    count += 1
        await query.edit_message_text(f"🗑 Deleted {count} temp folders.")
        return
    action, path = query.data.split("|", 1)
    if action == "tmp_del":
        try:
            shutil.rmtree(path, ignore_errors=True)
            await query.edit_message_text(f"🗑 Deleted: {path}")
        except Exception as e:
            await query.edit_message_text(f"Error deleting:\n{e}")
    elif action == "tmp_upload":
        # Find the largest file in the folder and upload it
        try:
            candidates = []
            for root, _, fs in os.walk(path):
                for f in fs:
                    fp = os.path.join(root, f)
                    try:
                        candidates.append((fp, os.path.getsize(fp)))
                    except OSError:
                        pass
            if not candidates:
                await query.edit_message_text("No files found in folder.")
                return
            candidates.sort(key=lambda x: -x[1])
            file_path = candidates[0][0]
            file_name = os.path.basename(file_path)

            status_msg = await query.message.reply_text(f"Uploading {file_name}\n{_progress_bar(0)}")
            loop = asyncio.get_event_loop()
            last_upload_update = [0]

            def upload_progress(percent):
                now = time.time()
                if now - last_upload_update[0] >= 1:
                    last_upload_update[0] = now
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(status_msg,f"Uploading {file_name}\n{_progress_bar(percent)}"),
                        loop,
                    )

            from drive_service import upload_to_drive
            _setup_auth_callback(update, loop)
            file_id, drive_link = await asyncio.to_thread(
                upload_to_drive,
                file_path=file_path,
                file_name=file_name,
                progress_callback=upload_progress,
            )
            await safe_edit(status_msg, f"Uploading {file_name}\n{_progress_bar(100)}")
            link_msg = await query.message.reply_text(f"✅ Uploaded: {drive_link}")
            shutil.rmtree(path, ignore_errors=True)

            ext = os.path.splitext(file_name)[1].lower()
            if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
                asyncio.create_task(_poll_video_ready(update, file_id, file_name, link_msg))
        except Exception as e:
            await query.message.reply_text(f"Upload failed:\n{e}")


async def ytcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("🔍 Checking YouTube access...")
    try:
        test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

        def do_check():
            return subprocess.run(
                [shutil.which("yt-dlp") or "/home/ms/telegram-yt-drive-bot/venv/bin/yt-dlp",
                 "--dump-json", "--no-playlist", "--no-download", test_url],
                capture_output=True, text=True, timeout=60
            )

        result = await asyncio.to_thread(do_check)

        if result.returncode == 0:
            await safe_edit(msg, "✅ YouTube is working normally. No block detected.")
        elif "403" in result.stderr or "Forbidden" in result.stderr:
            await safe_edit(msg, "🚫 YouTube is blocking downloads from this IP.\n\nThis is usually temporary (a few hours to 24h). Caused by too many requests.")
        elif "reloaded" in result.stderr:
            await safe_edit(msg, "🚫 YouTube is blocking this IP.\n\nThis is usually temporary (a few hours to 24h). Caused by too many requests.")
        else:
            err = result.stderr[-300:] if result.stderr else "Unknown error"
            await safe_edit(msg, f"⚠️ YouTube test failed:\n{err}")
    except subprocess.TimeoutExpired:
        await safe_edit(msg, "⚠️ YouTube test timed out. Connection may be slow or blocked.")
    except Exception as e:
        await safe_edit(msg, f"Error: {e}")


async def speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("⏳ Running speedtest...")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["speedtest-cli", "--simple"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            await safe_edit(msg, f"Speedtest failed:\n{result.stderr[:500]}")
            return
        lines = result.stdout.strip().split("\n")
        parts = {}
        for line in lines:
            if ":" in line:
                k, v = line.split(":", 1)
                parts[k.strip()] = v.strip()
        ping = parts.get("Ping", "?")
        down = parts.get("Download", "?")
        up = parts.get("Upload", "?")
        await safe_edit(msg,
            f"📡 Speedtest Results\n\n"
            f"🏓 Ping: {ping}\n"
            f"⬇️ Download: {down}\n"
            f"⬆️ Upload: {up}"
        )
    except subprocess.TimeoutExpired:
        await safe_edit(msg, "Speedtest timed out (2 min limit).")
    except FileNotFoundError:
        await safe_edit(msg, "speedtest-cli not installed.\nRun: pip install speedtest-cli")
    except Exception as e:
        await safe_edit(msg, f"Error: {e}")


async def storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        from drive_service import get_storage_info
        info = await asyncio.to_thread(get_storage_info)
        pct = round((info["used_gb"] / info["total_gb"]) * 100, 1) if info["total_gb"] else 0
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        await update.message.reply_text(
            f"📧 {info['email']}\n"
            f"💾 {info['used_gb']} GB / {info['total_gb']} GB\n"
            f"📦 Free: {info['free_gb']} GB\n"
            f"[{bar}] {pct}%"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not fetch storage info:\n{e}")


_uploads_cache: dict[int, list] = {}


async def _build_uploads_list(files):
    lines = ["📁 Last 10 uploads\n"]
    for i, f in enumerate(files, 1):
        size = _format_size(f.get("size"))
        name = f.get("name", "Unknown")
        lines.append(f"  {i}.  {name}\n       {size}")
    buttons = [[InlineKeyboardButton(f"{i}", callback_data=f"upl_pick_{i-1}")] for i in range(1, len(files) + 1)]
    rows = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
    flat_rows = [[btn for sublist in row for btn in sublist] for row in rows]
    return "\n".join(lines), InlineKeyboardMarkup(flat_rows)


def _build_file_detail(f):
    name = f.get("name", "Unknown")
    size = _format_size(f.get("size"))
    fid = f["id"]
    link = f"https://drive.google.com/file/d/{fid}/view"
    text = f"📄 {name}\n📦 {size}\n🔗 {link}"
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"upl_ren_{fid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"upl_del_{fid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="upl_back")],
    ])
    return text, buttons


async def uploads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("Loading...")
    try:
        from drive_service import list_recent_files
        files = await asyncio.to_thread(list_recent_files, 10)
        if not files:
            await safe_edit(msg, "No files found in Drive folder.")
            return
        uid = update.effective_user.id
        _uploads_cache[uid] = files
        text, markup = await _build_uploads_list(files)
        await safe_edit(msg, text, reply_markup=markup)
    except Exception as e:
        await safe_edit(msg, f"Error: {e}")


async def uploads_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("upl_pick_"):
        idx = int(data[9:])
        files = _uploads_cache.get(uid, [])
        if idx < len(files):
            text, markup = _build_file_detail(files[idx])
            await query.edit_message_text(text, reply_markup=markup)

    elif data == "upl_back":
        files = _uploads_cache.get(uid, [])
        if files:
            text, markup = await _build_uploads_list(files)
            await query.edit_message_text(text, reply_markup=markup)

    elif data.startswith("upl_del_"):
        file_id = data[8:]
        try:
            from drive_service import delete_file
            await asyncio.to_thread(delete_file, file_id)
            _uploads_cache[uid] = [f for f in _uploads_cache.get(uid, []) if f["id"] != file_id]
            files = _uploads_cache[uid]
            if files:
                text, markup = await _build_uploads_list(files)
                await query.edit_message_text(f"✅ Deleted.\n\n{text}", reply_markup=markup)
            else:
                await query.edit_message_text("✅ Deleted. No more files.")
        except Exception as e:
            await query.edit_message_text(f"Delete failed: {e}")

    elif data.startswith("upl_ren_"):
        file_id = data[8:]
        pending_renames[uid] = file_id
        await query.edit_message_text("✏️ Send me the new file name:")


async def post_init(application):
    """Set the bot's command menu button."""
    from telegram import BotCommandScopeDefault, BotCommandScopeAllPrivateChats
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("quality", "Choose download quality"),
        BotCommand("clip", "Download a clip: /clip START END LINK"),
        BotCommand("combine", "Combine multiple clips into one video"),
        BotCommand("retry", "Re-upload a failed file: /retry FOLDER_PATH"),
        BotCommand("cancel", "Cancel the current task"),
        BotCommand("login", "Sign in to Google Drive"),
        BotCommand("logout", "Sign out of Google Drive"),
        BotCommand("status", "Show logged in Google account"),
        BotCommand("files", "Show temp files and disk usage"),
        BotCommand("storage", "Show Google Drive storage usage"),
        BotCommand("speedtest", "Run a speedtest on the Pi"),
        BotCommand("ytcheck", "Check if YouTube is blocking downloads"),
        BotCommand("uploads", "Show last 10 uploaded files on Drive"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())


def main():
    request = HTTPXRequest(read_timeout=600, write_timeout=600, connect_timeout=30)
    app = Application.builder().token(BOT_TOKEN).request(request).base_url("http://localhost:8081/bot").base_file_url("http://localhost:8081/file/bot").local_mode(True).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quality", set_quality))
    app.add_handler(CommandHandler("clip", clip))
    app.add_handler(CommandHandler("combine", combine))
    app.add_handler(CommandHandler("retry", retry))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("storage", storage))
    app.add_handler(CommandHandler("speedtest", speedtest))
    app.add_handler(CommandHandler("ytcheck", ytcheck))
    app.add_handler(CommandHandler("uploads", uploads_cmd))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^quality_"))
    app.add_handler(CallbackQueryHandler(tmp_callback, pattern=r"^tmp_"))
    app.add_handler(CallbackQueryHandler(uploads_callback, pattern=r"^upl_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.PHOTO,
        handle_file,
    ))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
