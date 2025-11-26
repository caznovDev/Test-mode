from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import yt_dlp
import os
import uuid
import shutil
import zipfile
from pathlib import Path
from typing import List, Optional
import re

app = FastAPI(title="Rumble Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RumbleRequest(BaseModel):
    page_url: str
    
    @field_validator('page_url')
    @classmethod
    def validate_url(cls, v):
        if not v or not v.strip():
            raise ValueError('page_url must be a non-empty string')
        
        url_pattern = re.compile(
            r'^https?://'
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
            r'localhost|'
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
            r'(?::\d+)?'
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
        
        if not url_pattern.match(v.strip()):
            raise ValueError('page_url must be a valid HTTP/HTTPS URL')
        
        return v.strip()


def cleanup_temp_files(temp_dir: str, zip_path: str):
    """Background task to clean up temporary files and directories"""
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"✓ Cleaned up temp directory: {temp_dir}")
        
        if os.path.exists(zip_path):
            os.remove(zip_path)
            print(f"✓ Cleaned up ZIP file: {zip_path}")
    except Exception as e:
        print(f"✗ Error during cleanup: {str(e)}")


def extract_video_urls(page_url: str, max_videos: int = 10) -> List[str]:
    """Extract video URLs from a Rumble page using yt-dlp"""
    print(f"→ Extracting videos from: {page_url}")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'playlistend': max_videos,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(page_url, download=False)
            
            if not result:
                raise Exception("No data extracted from page")
            
            video_urls = []
            
            if 'entries' in result:
                for entry in result['entries']:
                    if entry and 'url' in entry:
                        video_urls.append(entry['url'])
                    elif entry and 'id' in entry:
                        video_urls.append(f"https://rumble.com/{entry['id']}")
            elif 'url' in result:
                video_urls.append(result['url'])
            elif 'id' in result:
                video_urls.append(f"https://rumble.com/{result['id']}")
            
            print(f"✓ Found {len(video_urls)} video(s)")
            return video_urls[:max_videos]
            
    except Exception as e:
        print(f"✗ Error extracting videos: {str(e)}")
        raise


def download_video(video_url: str, output_path: str, video_number: int) -> bool:
    """Download a single video using yt-dlp"""
    print(f"→ Downloading video {video_number}: {video_url}")
    
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
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
        
        partial_extensions = ['.part', '.ytdl', '.temp', '.download']
        for ext in partial_extensions:
            partial_file = output_path + ext
            if os.path.exists(partial_file):
                os.remove(partial_file)
                print(f"  → Removed partial file: {partial_file}")
        
        return False


def create_zip_file(source_dir: str, zip_path: str) -> int:
    """Create a ZIP file from all files in the source directory"""
    file_count = 0
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.basename(file)
                zipf.write(file_path, arcname)
                file_count += 1
    
    return file_count


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


@app.post("/api/rumble-zip")
async def download_rumble_videos(request: RumbleRequest, background_tasks: BackgroundTasks):
    """
    Download videos from a Rumble page and return them as a ZIP file
    
    Args:
        request: Contains page_url - the Rumble page URL to download from
        
    Returns:
        ZIP file containing downloaded videos
    """
    page_url = request.page_url
    print(f"\n{'='*60}")
    print(f"📥 New request received")
    print(f"   Page URL: {page_url}")
    print(f"{'='*60}")
    
    random_id = str(uuid.uuid4())
    temp_dir = os.path.join("tmp", random_id)
    zip_path = f"tmp/{random_id}.zip"
    
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs("tmp", exist_ok=True)
    
    try:
        try:
            video_urls = extract_video_urls(page_url, max_videos=10)
        except Exception as e:
            print(f"✗ Failed to extract videos from page")
            raise HTTPException(
                status_code=502,
                detail="failed to extract videos from page"
            )
        
        if not video_urls:
            print(f"✗ No videos found on page")
            raise HTTPException(
                status_code=400,
                detail="no videos found on page"
            )
        
        print(f"→ Starting download of {len(video_urls)} video(s)")
        successful_downloads = 0
        
        for idx, video_url in enumerate(video_urls, 1):
            output_path = os.path.join(temp_dir, f"video{idx:02d}.mp4")
            
            if download_video(video_url, output_path, idx):
                successful_downloads += 1
        
        print(f"✓ Successfully downloaded {successful_downloads}/{len(video_urls)} video(s)")
        
        if successful_downloads == 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"✗ No videos were successfully downloaded")
            raise HTTPException(
                status_code=500,
                detail="could not download any videos"
            )
        
        print(f"→ Creating ZIP file...")
        file_count = create_zip_file(temp_dir, zip_path)
        print(f"✓ ZIP file created with {file_count} file(s)")
        
        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        print(f"✓ ZIP size: {zip_size_mb:.2f} MB")
        print(f"{'='*60}\n")
        
        background_tasks.add_task(cleanup_temp_files, temp_dir, zip_path)
        
        return FileResponse(
            path=zip_path,
            media_type="application/zip",
            filename="rumble_batch.zip"
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
            detail=f"internal server error: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
