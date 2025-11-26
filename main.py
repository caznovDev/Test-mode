from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import yt_dlp
import os
import uuid
import shutil
import zipfile
from pathlib import Path
from typing import List
import re
import boto3
import tempfile


# ============================================================
#  FASTAPI + CORS
# ============================================================

app = FastAPI(title="Rumble Video Downloader API with R2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
#  WRITABLE TEMP DIRECTORY (Vercel-safe)
# ============================================================

BASE_TMP_DIR = "/tmp/rumble-api"  # Vercel allows writing here
os.makedirs(BASE_TMP_DIR, exist_ok=True)

# ============================================================
#  Cloudflare R2 Config (PLACEHOLDER values provided by you)
#  Move these to env vars later.
# ============================================================

R2_ACCOUNT_ID = "42d73c812e25ae65e416d5f503b18be4"
R2_ACCESS_KEY_ID = "ad21a412d42c22b376cef4e7b4a7381b"
R2_SECRET_ACCESS_KEY = "d058f7d8e0aebeb8644868ff57a7139a309015aa3a5e7e8ec031670a35403a2f"
R2_BUCKET = "files"
R2_S3_ENDPOINT = "https://42d73c812e25ae65e416d5f503b18be4.r2.cloudflarestorage.com"
R2_PUBLIC_BASE_URL = "https://pub-2088e0ca945a43f996f9d7be86ec3dc5.r2.dev"

# boto3 client for R2
r2_session = boto3.session.Session()
s3_client = r2_session.client(
    service_name="s3",
    endpoint_url=R2_S3_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
)

# ============================================================
#  Pydantic Models
# ============================================================

class RumbleRequest(BaseModel):
    page_url: str

    @field_validator('page_url')
    def check_url(cls, v):
        if not v or not v.strip():
            raise ValueError("page_url must be non-empty")
        pattern = re.compile(
            r"^https?://"
            r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
            r"localhost|"
            r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
            r"(?::\d+)?"
            r"(?:/?|[/?]\S+)$",
            re.IGNORECASE,
        )
        if not pattern.match(v.strip()):
            raise ValueError("Invalid page_url")
        return v.strip()


class RumbleRequestWithLimit(RumbleRequest):
    max_videos: int = 10

    @field_validator("max_videos")
    def check_limit(cls, v):
        if v < 1 or v > 10:
            raise ValueError("max_videos must be between 1 and 10")
        return v


# ============================================================
#  Helpers
# ============================================================

def cleanup_temp_files(temp_dir: str, zip_path: str):
    """Delete temp directory + ZIP after response is sent."""
    try:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"✓ Cleaned temp dir: {temp_dir}")

        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
            print(f"✓ Deleted zip: {zip_path}")
    except Exception as e:
        print(f"Cleanup error: {e}")


def extract_video_urls(page_url: str, max_videos: int) -> List[str]:
    """Use yt-dlp to extract first N video URLs."""
    print(f"→ Extracting URLs from: {page_url}")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": max_videos,
        "retries": 3,
        "socket_timeout": 15,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except Exception as e:
        print("✗ yt-dlp extraction failed:", e)
        raise

    urls = []
    if "entries" in info and info["entries"]:
        for entry in info["entries"]:
            if entry and "url" in entry:
                urls.append(entry["url"])
    elif "url" in info:
        urls.append(info["url"])

    print(f"✓ Found {len(urls)} videos")
    return urls[:max_videos]


def download_video(video_url: str, output_path: str, idx: int) -> bool:
    """Download 1 video using yt-dlp."""
    print(f"→ Downloading video {idx}: {video_url}")

    opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 25,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        return True

    except Exception as e:
        print(f"✗ Download failed for video {idx}: {e}")
        # remove partial files
        for ext in ["", ".part", ".ytdl", ".temp", ".download"]:
            if os.path.exists(output_path + ext):
                os.remove(output_path + ext)
        return False


def create_zip(src_dir: str, zip_path: str) -> int:
    """Zip all MP4 files in the directory."""
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(src_dir):
            for f in files:
                file_path = os.path.join(root, f)
                zipf.write(file_path, arcname=f)
                count += 1
    return count


def upload_to_r2(zip_path: str, key: str) -> str:
    """Upload ZIP to R2 and return the public URL."""
    print(f"→ Uploading ZIP to R2 with key: {key}")

    try:
        s3_client.upload_file(zip_path, R2_BUCKET, key)
    except Exception as e:
        print("✗ R2 upload failed:", e)
        raise

    public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    print("✓ Uploaded. Public URL:", public_url)
    return public_url


# ============================================================
#  ENDPOINTS
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/rumble-urls")
async def api_rumble_urls(req: RumbleRequestWithLimit):
    try:
        urls = extract_video_urls(req.page_url, req.max_videos)
        return {"count": len(urls), "video_urls": urls}
    except Exception:
        raise HTTPException(502, "Failed to extract video links")


@app.post("/api/rumble-zip")
async def api_rumble_zip(req: RumbleRequestWithLimit, bg: BackgroundTasks):

    # mkdir safe temp directory inside /tmp
    random_id = str(uuid.uuid4())
    temp_dir = os.path.join(BASE_TMP_DIR, random_id)
    zip_path = os.path.join(BASE_TMP_DIR, f"{random_id}.zip")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        # Extract
        urls = extract_video_urls(req.page_url, req.max_videos)
        if not urls:
            raise HTTPException(400, "No videos found on page")

        # Download
        success = 0
        for i, url in enumerate(urls, 1):
            out_path = os.path.join(temp_dir, f"video{i:02d}.mp4")
            if download_video(url, out_path, i):
                success += 1

        if success == 0:
            raise HTTPException(500, "Could not download any videos")

        # Zip
        count = create_zip(temp_dir, zip_path)
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)

        # Upload to R2
        r2_key = f"rumble_zips/{random_id}.zip"
        download_url = upload_to_r2(zip_path, r2_key)

        # Cleanup local files async
        bg.add_task(cleanup_temp_files, temp_dir, zip_path)

        return JSONResponse(
            {
                "job_id": random_id,
                "page_url": req.page_url,
                "file_count": count,
                "zip_size_mb": round(size_mb, 2),
                "download_url": download_url,
                "status": "ready",
            }
        )

    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        print("Unexpected error:", e)
        raise HTTPException(500, f"Internal error: {e}")


# ============================================================
#  LOCAL DEV SERVER (ignored by Vercel runtime)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
