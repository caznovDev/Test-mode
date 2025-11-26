from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import yt_dlp
import boto3
import requests
import re
import uuid
from typing import List, Dict, Any

# ============================================================
#  FASTAPI + CORS
# ============================================================

app = FastAPI(title="Rumble → R2 Streamer API")

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
R2_PUBLIC_BASE_URL = "https://pub-2088e0ca945a43f996f9d7be86ec3dc5.r2.dev"

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

    @field_validator("page_url")
    def validate_url(cls, v: str) -> str:
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
    # Default small to stay well inside Vercel time limits & no disk use
    max_videos: int = 3

    @field_validator("max_videos")
    def validate_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_videos must be at least 1")
        # Streaming multiple videos still takes time; keep this conservative
        if v > 5:
            raise ValueError("max_videos must be <= 5 on this deployment")
        return v

# ============================================================
#  yt-dlp Helpers (no download, no cache)
# ============================================================

def extract_page_video_urls(page_url: str, max_videos: int) -> List[str]:
    """
    Use yt-dlp in 'flat' mode to get the first N *page* URLs from a Rumble
    listing page (user/videos, playlist, etc.).
    """
    print(f"→ Extracting page URLs from: {page_url} (max {max_videos})")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": max_videos,
        "retries": 3,
        "socket_timeout": 15,
        "skip_download": True,
        "cachedir": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(page_url, download=False)

    urls: List[str] = []

    if "entries" in info and info["entries"]:
        for entry in info["entries"]:
            if not entry:
                continue
            if "url" in entry:
                # For flat playlist entries, 'url' is usually the page URL
                urls.append(entry["url"])
    else:
        # Single video / non-playlist page
        if "webpage_url" in info:
            urls.append(info["webpage_url"])
        elif "url" in info:
            urls.append(info["url"])

    print(f"✓ Found {len(urls)} page URLs")
    return urls[:max_videos]


def get_best_direct_video_url(video_page_url: str) -> Dict[str, Any]:
    """
    Given a Rumble video *page* URL, use yt-dlp to get the info dict and pick
    a direct media URL (prefer MP4).
    Returns a dict containing:
      - 'direct_url'
      - 'ext'
      - 'id'
      - 'title'
      - 'duration'
    """
    print(f"→ Getting direct video URL for page: {video_page_url}")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cachedir": False,
        "retries": 3,
        "socket_timeout": 20,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_page_url, download=False)

    # If the top-level info already has a direct URL and ext, use it
    if "url" in info and info.get("ext"):
        return {
            "direct_url": info["url"],
            "ext": info.get("ext", "mp4"),
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": info.get("duration"),
        }

    formats = info.get("formats") or []
    if not formats:
        raise RuntimeError("No formats found for video")

    # Prefer MP4 formats, choose highest resolution/bitrate
    mp4_formats = [f for f in formats if f.get("ext") == "mp4" and f.get("url")]
    candidate_formats = mp4_formats or [f for f in formats if f.get("url")]

    if not candidate_formats:
        raise RuntimeError("No usable formats with URLs found for video")

    def score(f):
        # sort by (height, tbr) descending
        h = f.get("height") or 0
        tbr = f.get("tbr") or 0
        return (h, tbr)

    candidate_formats.sort(key=score, reverse=True)
    best = candidate_formats[0]

    return {
        "direct_url": best["url"],
        "ext": best.get("ext", "mp4"),
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
    }

# ============================================================
#  R2 Streaming Helper
# ============================================================

def stream_video_to_r2(media_url: str, r2_key: str) -> None:
    """
    Stream a remote media URL directly into R2 without saving to disk.
    """
    print(f"→ Streaming to R2 key: {r2_key}")
    with requests.get(media_url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        # resp.raw is a file-like object
        s3_client.upload_fileobj(resp.raw, R2_BUCKET, r2_key)
    print("✓ Upload to R2 completed")

def build_public_r2_url(key: str) -> str:
    return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key.lstrip('/')}"

# ============================================================
#  ENDPOINTS
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/rumble-urls")
async def api_rumble_urls(req: RumbleRequestWithLimit):
    """
    Lightweight helper: returns the first N video PAGE URLs from the Rumble listing.
    """
    try:
        urls = extract_page_video_urls(req.page_url, req.max_videos)
        if not urls:
            raise HTTPException(400, "No video URLs found on page")
        return {"count": len(urls), "video_page_urls": urls}
    except HTTPException:
        raise
    except Exception as e:
        print("Error in /api/rumble-urls:", e)
        raise HTTPException(502, "Failed to extract URLs from Rumble page")


@app.post("/api/rumble-r2")
async def api_rumble_r2(req: RumbleRequestWithLimit):
    """
    Main endpoint:
      1) Extract first N (<=5) video *page* URLs from the given Rumble page.
      2) For each page URL, get the best direct media URL via yt-dlp.
      3) Stream each media URL directly into R2 (no local disk).
      4) Return JSON with the list of R2 public URLs.
    """
    job_id = str(uuid.uuid4())
    print("\n" + "=" * 60)
    print("📥 New R2 job")
    print(f"   Job ID: {job_id}")
    print(f"   Page URL: {req.page_url}")
    print(f"   Max videos: {req.max_videos}")
    print("=" * 60)

    try:
        page_urls = extract_page_video_urls(req.page_url, req.max_videos)
        if not page_urls:
            raise HTTPException(400, "No videos found on page")

        results = []
        for idx, page_url in enumerate(page_urls, 1):
            entry: Dict[str, Any] = {
                "index": idx,
                "page_url": page_url,
                "success": False,
                "error": None,
                "r2_key": None,
                "r2_url": None,
                "duration": None,
                "title": None,
            }
            try:
                info = get_best_direct_video_url(page_url)
                media_url = info["direct_url"]
                ext = info.get("ext", "mp4") or "mp4"
                vid_id = info.get("id") or f"video{idx:02d}"

                r2_key = f"rumble_streams/{job_id}/{vid_id}.{ext}"
                stream_video_to_r2(media_url, r2_key)
                r2_url = build_public_r2_url(r2_key)

                entry["success"] = True
                entry["r2_key"] = r2_key
                entry["r2_url"] = r2_url
                entry["duration"] = info.get("duration")
                entry["title"] = info.get("title")
            except Exception as e:
                print(f"✗ Failed to process video {idx}: {e}")
                entry["error"] = str(e)

            results.append(entry)

        ok_count = sum(1 for r in results if r["success"])
        print(f"✓ Job {job_id} finished: {ok_count}/{len(results)} uploads succeeded")

        if ok_count == 0:
            raise HTTPException(502, "Failed to upload any videos to R2")

        return JSONResponse(
            {
                "job_id": job_id,
                "page_url": req.page_url,
                "max_videos": req.max_videos,
                "success_count": ok_count,
                "total": len(results),
                "entries": results,
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print("Unexpected error in /api/rumble-r2:", e)
        raise HTTPException(500, f"Internal error: {e}")

# ============================================================
#  LOCAL DEV SERVER (ignored by Vercel)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
