import os
import certifi
import threading

os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secret.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")

DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# Set from bot.py — sends auth URL to user via Telegram
auth_url_callback = None

# For receiving the redirect URL back from the user
auth_response_event = threading.Event()
auth_response_value = None


def set_auth_response(value):
    """Called from bot.py when user sends the redirect URL."""
    global auth_response_value
    auth_response_value = value
    auth_response_event.set()


# Flag to indicate we're waiting for auth
waiting_for_auth = False


def _get_service():
    global auth_response_value, waiting_for_auth
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)

            if auth_url_callback:
                # Use a fixed redirect URI pointing to localhost
                redirect_uri = "http://localhost:1/"
                flow.redirect_uri = redirect_uri
                auth_url, _ = flow.authorization_url(
                    access_type="offline", prompt="consent"
                )
                auth_url_callback(auth_url)

                # Wait for user to paste the redirect URL
                auth_response_event.clear()
                auth_response_value = None
                waiting_for_auth = True
                auth_response_event.wait(timeout=300)
                waiting_for_auth = False

                if not auth_response_value:
                    raise Exception("Google sign-in timed out (5 min). Please try again.")

                flow.fetch_token(
                    authorization_response=auth_response_value.replace("http://", "https://")
                )
                creds = flow.credentials
            else:
                creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_drive(file_path: str, file_name: str, progress_callback=None) -> str:
    """Upload a file to Google Drive, make it public, and return the link."""
    service = _get_service()

    file_metadata = {"name": file_name}
    if DRIVE_FOLDER_ID:
        file_metadata["parents"] = [DRIVE_FOLDER_ID]

    media = MediaFileUpload(file_path, resumable=True)
    request = service.files().create(
        body=file_metadata, media_body=media, fields="id"
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_callback:
            progress_callback(int(status.progress() * 100))

    file_id = response["id"]

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return file_id, f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def check_video_ready(file_id: str) -> bool:
    """Check if a video on Google Drive has finished processing and is viewable."""
    service = _get_service()
    f = service.files().get(
        fileId=file_id, fields="videoMediaMetadata,mimeType"
    ).execute()
    vmm = f.get("videoMediaMetadata")
    if vmm and vmm.get("durationMillis"):
        return True
    return False


def list_recent_files(count=10) -> list:
    service = _get_service()
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false" if DRIVE_FOLDER_ID else "trashed=false"
    results = service.files().list(
        q=query,
        orderBy="createdTime desc",
        pageSize=count,
        fields="files(id,name,size,createdTime,mimeType)",
    ).execute()
    return results.get("files", [])


def rename_file(file_id: str, new_name: str) -> str:
    service = _get_service()
    updated = service.files().update(
        fileId=file_id,
        body={"name": new_name},
        fields="name",
    ).execute()
    return updated["name"]


def delete_file(file_id: str):
    service = _get_service()
    service.files().delete(fileId=file_id).execute()


def get_storage_info() -> dict:
    service = _get_service()
    about = service.about().get(fields="storageQuota,user").execute()
    quota = about.get("storageQuota", {})
    user = about.get("user", {})
    total = int(quota.get("limit", 0))
    used = int(quota.get("usage", 0))
    free = total - used
    return {
        "email": user.get("emailAddress", "Unknown"),
        "total_gb": round(total / (1024**3), 2),
        "used_gb": round(used / (1024**3), 2),
        "free_gb": round(free / (1024**3), 2),
    }
