# YouTube to Google Drive Telegram Bot

A Telegram bot that downloads YouTube videos and uploads them to your Google Drive.

## Features
- Download YouTube videos in any quality (360p to 4K)
- Download audio only (MP3)
- Clip specific portions of videos (`/clip 1:35 4:24 <link>`)
- Upload files/videos sent directly to the bot to Google Drive
- Progress bars for download and upload
- Notifies you when uploaded videos are watchable on Drive

## Setup Instructions

### 1. Install Python
- Download and install Python 3.10+ from https://python.org
- During installation, check "Add Python to PATH"

### 2. Install FFmpeg
- Open a terminal and run:
  ```
  winget install Gyan.FFmpeg
  ```
- Or download from https://ffmpeg.org/download.html and add to PATH

### 3. Install Deno (required for YouTube downloads)
- Open a terminal and run:
  ```
  winget install DenoLand.Deno
  ```
- Then run this once to cache the YouTube solver script:
  ```
  yt-dlp --remote-components ejs:github
  ```

### 4. Create a Telegram Bot
1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow the prompts to create your bot
3. Copy the **bot token** (looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 5. Set Up Google Drive API
1. Go to https://console.cloud.google.com/
2. Create a new project (or select an existing one)
3. Enable the **Google Drive API**:
   - Go to "APIs & Services" > "Library"
   - Search for "Google Drive API" and click "Enable"
4. Create OAuth credentials:
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "OAuth client ID"
   - If prompted, configure the OAuth consent screen first:
     - Choose "External" user type
     - Fill in app name and your email
     - Add your email as a test user
   - Application type: **Desktop app**
   - Click "Create"
5. Download the credentials JSON file
6. Rename it to `client_secret.json` and place it in the bot folder

### 6. Find Your Telegram User ID
1. Open Telegram and search for `@userinfobot`
2. Send it any message - it will reply with your user ID
3. Copy the numeric ID

### 7. Configure the Bot
1. Create a file called `.env` in the bot folder with this content:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   GOOGLE_DRIVE_FOLDER_ID=
   ALLOWED_USER_IDS=your_telegram_user_id_here
   ```
   - `TELEGRAM_BOT_TOKEN`: The token from BotFather (step 4)
   - `GOOGLE_DRIVE_FOLDER_ID`: (Optional) A specific Google Drive folder ID to upload into. Leave empty to upload to root.
   - `ALLOWED_USER_IDS`: Your Telegram user ID (step 6). For multiple users, separate with commas: `123456,789012`

### 8. Install Python Dependencies
Open a terminal in the bot folder and run:
```
pip install -r requirements.txt
```

### 9. Run the Bot
Double-click `start_bot.bat` or run:
```
python bot.py
```

On the first run, a browser window will open asking you to sign in with your Google account and grant Drive access. This only happens once.

## Usage

- **Send a YouTube link** - Downloads and uploads to Drive
- **/quality** - Choose download quality (360p-4K or MP3)
- **/clip START END LINK** - Download a specific portion
  - Example: `/clip 1:35 4:24 https://youtu.be/abc123`
- **Send a file/video/photo** - Uploads directly to Drive (max 20MB)

## Files to Share

Share these files (do NOT share `.env`, `token.json`, or `client_secret.json`):

```
bot.py
drive_service.py
requirements.txt
start_bot.bat
README.md
.env.example
```

## Troubleshooting

- **"invalid_grant" error**: Delete `token.json` and restart the bot. A browser will open to re-authenticate.
- **"Video not available" error**: Make sure Deno is installed and run `yt-dlp --remote-components ejs:github` once.
- **Bot not responding**: Make sure only one instance is running. Check Task Manager for extra `python.exe` processes.
