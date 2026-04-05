import os
import certifi

os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secret.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")

# Optional: set a specific folder ID to upload into.
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")


def _get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_drive(file_path: str, file_name: str, progress_callback=None) -> str:
    """Upload a file to Google Drive, make it public, and return the link.

    progress_callback: optional callable(percent: int) called with upload progress.
    """
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

    # Make the file publicly accessible (anyone with the link can view)
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
    # Once videoMediaMetadata has durationMillis, the video is processed
    vmm = f.get("videoMediaMetadata")
    if vmm and vmm.get("durationMillis"):
        return True
    return False
