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

# ============================================================
#  FastAPI app + CORS
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
#  Cloudflare R2 Config (PLACEHOLDERS - move to env later)
# ============================================================

R2_ACCOUNT_ID = "42d73c812e25ae65e416d5f503b18be4"
R2_ACCESS_KEY_ID = "ad21a412d42c22b376cef4e7b4a7381b"
R2_SECRET_ACCESS_KEY = "d058f7d8e0aebeb8644868ff57a7139a309015aa3a5e7e8ec031670a35403a2f"
R2_BUCKET = "files"
R2_S3_ENDPOINT = "https://42d73c812e25ae65e416d5f503b18be4.r2.cloudflarestorage.com"

# This is the public base URL you gave. We’ll build:
#   {R2_PUBLIC_BASE_URL}/{key}
R2_PUBLIC_BASE_URL = "https://pub-2088e0ca945a43f996f9d7be86ec3dc5.r2.dev"

# --- In production, use:
# R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
# R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
# R2_BUCKET = os.getenv("R2_BUCKET", "files")
# R2_S3_ENDPOINT = os.getenv("R2_S3_ENDPOINT")
# R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL")


# Create a single R2 (S3-compatible) client
r2_session = boto3.session.Session()
s3_client = r2_session.client(
    service_name="s3",
    endpoint_url=R2_S3_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
)


# ============================================================
#  Pydantic models
# ============================================================

class RumbleRequest(BaseModel):
    page_url: str

    @field_validator('page_url')
    @classmethod
    def validate_url(cls, v: str):
        if not v or not v.strip():
            raise ValueError('page_url must be a non-empty string')

        url_pattern = re.compile(
            r'^https?://'
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
            r'localhost|'
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
            r'(?::\d+)?'
            r'(?:/?|[/?]\S+)$',
            re.IGNORECASE
        )

        if not url_pattern.match(v.strip()):
            raise ValueError('page_url must be a valid HTTP/HTTPS URL')

        return v.strip()


class RumbleRequestWithLimit(RumbleRequest):
    max_videos: int = 10

    @field_validator("max_videos")
    @classmethod
    def validate_max_videos(cls, v: int):
        if v < 1:
            raise ValueError("max_videos must be at least 1")
        if v > 10:
            # to keep function runtime reasonable on Vercel
            raise ValueError("max_videos must be <= 10")
        return v


# ============================================================
#  Helpers: cleanup, extraction, download, zip, R2 upload
# ============================================================

def cleanup_temp_files(temp_dir: str, zip_path: str):
    """Background task to clean local temp files."""
    try:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"✓ Cleaned up temp directory: {temp_dir}")

        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
            print(f"✓ Cleaned up ZIP file: {zip_path}")
    except Exception as e:
        print(f"✗ Error during cleanup: {str(e)}")


def extract_video_urls(page_url: str, max_videos: int = 10) -> List[str]:
    """Extract video URLs from a Rumble page via yt-dlp (flat playlist)."""
    print(f"→ Extracting videos from: {page_url} (max {max_videos})")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": max_videos,
        "retries": 3,
        "socket_timeout": 15,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(page_url, download=False)

        if not result:
            raise Exception("No data extracted from page")

        video_urls: List[str] = []

        if "entries" in result and result["entries"]:
            for entry in result["entries"]:
                if not entry:
                    continue
                if "url" in entry:
                    video_urls.append(entry["url"])
                elif "id" in entry:
                    video_urls.append(f"https://rumble.com/{entry['id']}")
        else:
            if "url" in result:
                video_urls.append(result["url"])
            elif "id" in result:
                video_urls.append(f"https://rumble.com/{result['id']}")

        print(f"✓ Found {len(video_urls)} video(s)")
        return video_urls[:max_videos]

    except Exception as e:
        print(f"✗ Error extracting videos: {str(e)}")
        raise


def download_video(video_url: str, output_path: str, video_number: int) -> bool:
    """Download a single video using yt-dlp."""
    print(f"→ Downloading video {video_number}: {video_url}")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        print(f"✓ Successfully downloaded video {video_number}")
        return True

    except Exception as e:
        print(f"✗ Failed to download video {video_number}: {str(e)}")

        if os.path.exists(output_path):
            os.remove(output_path)
            print(f"  → Removed partial file: {output_path}")

        partial_extensions = [".part", ".ytdl", ".temp", ".download"]
        for ext in partial_extensions:
            partial_file = output_path + ext
            if os.path.exists(partial_file):
                os.remove(partial_file)
                print(f"  → Removed partial file: {partial_file}")

        return False


def create_zip_file(source_dir: str, zip_path: str) -> int:
    """Create a ZIP file from all files in source_dir."""
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.basename(file)
                zipf.write(file_path, arcname)
                file_count += 1
    return file_count


def upload_zip_to_r2(zip_path: str, key: str) -> str:
    """
    Upload the ZIP to Cloudflare R2 and return the public download URL.

    key example: "rumble_zips/<uuid>.zip"
    """
    print(f"→ Uploading ZIP to R2: bucket={R2_BUCKET}, key={key}")

    try:
        s3_client.upload_file(zip_path, R2_BUCKET, key)
        print("✓ ZIP uploaded to R2")
    except Exception as e:
        print(f"✗ Failed to upload ZIP to R2: {str(e)}")
        raise

    # Construct public URL using your r2.dev base
    public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    print(f"✓ Public URL: {public_url}")
    return public_url


# ============================================================
#  Endpoints
# ============================================================

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/rumble-urls")
async def get_rumble_video_urls(request: RumbleRequestWithLimit):
    """
    Lightweight endpoint: return up to max_videos URLs from the Rumble page.
    """
    page_url = request.page_url
    max_videos = request.max_videos

    print("\n" + "=" * 60)
    print("📥 New URL extraction request")
    print(f"   Page URL: {page_url}")
    print(f"   Max videos: {max_videos}")
    print("=" * 60)

    try:
        video_urls = extract_video_urls(page_url, max_videos=max_videos)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="failed to extract videos from page",
        )

    if not video_urls:
        raise HTTPException(status_code=400, detail="no videos found on page")

    return {"count": len(video_urls), "video_urls": video_urls}


@app.post("/api/rumble-zip")
async def download_rumble_videos(
    request: RumbleRequestWithLimit,
    background_tasks: BackgroundTasks,
):
    """
    Full pipeline:
      1) Extract URLs
      2) Download up to max_videos
      3) Zip them
      4) Upload ZIP to R2
      5) Cleanup local temp files
      6) Return JSON with R2 download_url
    """
    page_url = request.page_url
    max_videos = request.max_videos

    print("\n" + "=" * 60)
    print("📥 New ZIP job request")
    print(f"   Page URL: {page_url}")
    print(f"   Max videos: {max_videos}")
    print("=" * 60)

    os.makedirs("tmp", exist_ok=True)

    random_id = str(uuid.uuid4())
    temp_dir = os.path.join("tmp", random_id)
    zip_path = f"tmp/{random_id}.zip"
    os.makedirs(temp_dir, exist_ok=True)

    try:
        # 1) Extract URLs
        try:
            video_urls = extract_video_urls(page_url, max_videos=max_videos)
        except Exception:
            print("✗ Failed to extract videos from page")
            raise HTTPException(
                status_code=502,
                detail="failed to extract videos from page",
            )

        if not video_urls:
            print("✗ No videos found on page")
            raise HTTPException(
                status_code=400,
                detail="no videos found on page",
            )

        # 2) Download
        print(f"→ Starting download of {len(video_urls)} video(s)")
        successful_downloads = 0

        for idx, video_url in enumerate(video_urls, 1):
            output_path = os.path.join(temp_dir, f"video{idx:02d}.mp4")
            if download_video(video_url, output_path, idx):
                successful_downloads += 1

        print(f"✓ Successfully downloaded {successful_downloads}/{len(video_urls)}")

        if successful_downloads == 0:
            print("✗ No videos were successfully downloaded")
            raise HTTPException(
                status_code=500,
                detail="could not download any videos",
            )

        # 3) Zip
        print("→ Creating ZIP file...")
        file_count = create_zip_file(temp_dir, zip_path)
        print(f"✓ ZIP file created with {file_count} file(s)")

        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        print(f"✓ ZIP size: {zip_size_mb:.2f} MB")

        # 4) Upload to R2
        r2_key = f"rumble_zips/{random_id}.zip"
        download_url = upload_zip_to_r2(zip_path, r2_key)

        print("=" * 60 + "\n")

        # 5) Cleanup local files in background
        background_tasks.add_task(cleanup_temp_files, temp_dir, zip_path)

        # 6) Return JSON
        return JSONResponse(
            {
                "job_id": random_id,
                "page_url": page_url,
                "max_videos": max_videos,
                "file_count": file_count,
                "zip_size_mb": round(zip_size_mb, 2),
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
        print(f"✗ Unexpected error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"internal server error: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
