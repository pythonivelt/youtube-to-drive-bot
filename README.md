<div align="center">

# YouTube to Google Drive Bot

### A Telegram bot that downloads YouTube videos and uploads them to your Google Drive

<br/>

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white&labelColor=0d1117)](https://python.org)
[![Telegram Bot API](https://img.shields.io/badge/Telegram_Bot_API-21.6-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white&labelColor=0d1117)](https://python-telegram-bot.org)
[![Google Drive](https://img.shields.io/badge/Google_Drive_API-v3-4285F4?style=for-the-badge&logo=googledrive&logoColor=white&labelColor=0d1117)](https://developers.google.com/drive)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge&labelColor=0d1117)](LICENSE)

<br/>

[**Setup**](#setup) · [**Features**](#features) · [**Usage**](#usage) · [**Troubleshooting**](#troubleshooting)

</div>

---

<div align="center">

<h2><a id="features"></a>Features</h2>

<table>
  <tr>
    <td width="50%" valign="top">

### Download
- Download YouTube videos in any quality (360p to 4K)
- Download audio only as MP3
- Clip specific portions of videos
- Progress bar for all downloads

</td>
    <td width="50%" valign="top">

### Upload
- Automatic upload to your Google Drive
- Upload files sent directly to the bot (up to 20MB)
- Progress bar for uploads
- Get notified when video is watchable on Drive

</td>
  </tr>
  <tr>
    <td width="50%" valign="top">

### Quality Control
- Choose from 360p, 480p, 720p, 1080p, 1440p, 4K
- MP3 audio extraction at 192kbps
- Remembers your quality preference

</td>
    <td width="50%" valign="top">

### Security
- Restrict bot access to specific Telegram user IDs
- Your Google credentials stay on your machine
- No data stored on external servers

</td>
  </tr>
</table>

</div>

---

<div align="center">

<h2><a id="usage"></a>Usage</h2>

</div>

| Command | Description | Example |
|---------|-------------|---------|
| Send a YouTube link | Downloads and uploads to Drive | `https://youtu.be/abc123` |
| `/quality` | Choose download quality | Select from menu |
| `/clip START END LINK` | Download a specific portion | `/clip 1:35 4:24 https://youtu.be/abc123` |
| Send a file | Uploads directly to Drive (max 20MB) | Send any file, video, or photo |

---

<div align="center">

<h2><a id="setup"></a>Setup</h2>

</div>

### Prerequisites

| Software | How to Install |
|----------|---------------|
| **Python 3.10+** | [python.org](https://python.org) — check "Add Python to PATH" during install |
| **FFmpeg** | `winget install Gyan.FFmpeg` |
| **Deno** | `winget install DenoLand.Deno` then run `yt-dlp --remote-components ejs:github` once |

### Step 1 — Download the Bot

1. Click the green **Code** button at the top of this page
2. Click **Download ZIP**
3. Unzip the folder and place it wherever you want on your computer

### Step 2 — Create a Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Step 3 — Set Up Google Drive API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable the **Google Drive API** (APIs & Services → Library → search "Google Drive API")
4. Create OAuth credentials:
   - Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Configure the consent screen if prompted (External, add your email as test user)
   - Application type: **Desktop app**
5. Download the credentials JSON file
6. **Rename it to `client_secret.json`** and place it in the bot folder

### Step 4 — Get Your Telegram User ID

1. Open Telegram and search for `@userinfobot`
2. Send it any message — it will reply with your user ID

### Step 5 — Configure

1. Rename `.env.example` to `.env`
2. Fill in your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
GOOGLE_DRIVE_FOLDER_ID=
ALLOWED_USER_IDS=your_telegram_user_id_here
```

> `GOOGLE_DRIVE_FOLDER_ID` is optional. Leave empty to upload to your Drive root.
> For multiple users, separate IDs with commas: `123456,789012`

### Step 6 — Install & Run

```bash
pip install -r requirements.txt
python bot.py
```

Or double-click **`start_bot_generic.bat`**

> On the first run, a browser window will open to sign in with your Google account. This only happens once.

---

<div align="center">

<h2><a id="troubleshooting"></a>Troubleshooting</h2>

</div>

| Problem | Solution |
|---------|----------|
| `invalid_grant` error | Delete `token.json` and restart the bot to re-authenticate |
| "Video not available" for public videos | Make sure Deno is installed and run `yt-dlp --remote-components ejs:github` |
| Bot not responding | Make sure only one instance is running (check Task Manager for extra `python.exe`) |
| SSL certificate errors | Run `pip install --upgrade certifi` |

---

<div align="center">

### Built with

[![yt-dlp](https://img.shields.io/badge/yt--dlp-FF0000?style=for-the-badge&logo=youtube&logoColor=white&labelColor=0d1117)](https://github.com/yt-dlp/yt-dlp)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white&labelColor=0d1117)](https://python-telegram-bot.org)
[![Google Drive API](https://img.shields.io/badge/Google_Drive_API-4285F4?style=for-the-badge&logo=googledrive&logoColor=white&labelColor=0d1117)](https://developers.google.com/drive)

</div>
