# Rumble Video Downloader API

## Overview
A FastAPI-based web service that downloads videos from Rumble user pages, packages them into ZIP files, and streams them to clients. Built specifically for Replit free tier with efficient resource management.

**Created:** November 26, 2025  
**Status:** Fully functional

## Purpose
- Extract up to 10 videos from any Rumble page containing video listings
- Download videos using yt-dlp
- Package downloaded videos into a ZIP file
- Return ZIP to client (e.g., Google Colab) via HTTP response
- Automatically clean up temporary files to manage disk space

## Recent Changes
- **Nov 26, 2025:** Initial implementation
  - Created FastAPI application with health check and video download endpoints
  - Integrated yt-dlp for video extraction and downloading
  - Implemented automatic cleanup using background tasks
  - Added comprehensive error handling and logging
  - Configured CORS for cross-origin requests

## Project Architecture

### Technology Stack
- **Framework:** FastAPI 0.109.0
- **Server:** Uvicorn 0.27.0
- **Video Downloader:** yt-dlp 2024.3.10
- **Python Version:** 3.11

### File Structure
```
.
├── main.py              # Main FastAPI application
├── requirements.txt     # Python dependencies
├── .gitignore          # Ignore patterns for temp files and videos
├── .replit             # Replit configuration
└── replit.md           # This file
```

### API Endpoints

#### GET /health
Health check endpoint for monitoring service availability.

**Response:**
```json
{
  "status": "ok"
}
```

#### POST /api/rumble-zip
Download videos from a Rumble page and return as ZIP file.

**Request Body:**
```json
{
  "page_url": "https://rumble.com/user/SomeUser/videos?page=8"
}
```

**Response:** Binary ZIP file (application/zip) named `rumble_batch.zip`

**Error Codes:**
- `400`: Invalid URL or no videos found
- `502`: Failed to extract videos from page
- `500`: Internal server error or download failure

### Key Features

1. **URL Validation:** Validates HTTP/HTTPS URLs before processing
2. **Video Extraction:** Uses yt-dlp to extract first 10 videos from any Rumble page
3. **Resilient Downloads:** Continues downloading even if individual videos fail
4. **Automatic Cleanup:** Background tasks remove temporary files after response
5. **Resource Efficient:** Designed for Replit free tier (1GB disk limit)
6. **CORS Enabled:** Allows cross-origin requests from Google Colab and other clients
7. **Detailed Logging:** Console output tracks all operations

### Workflow Configuration
- **Name:** FastAPI Server
- **Command:** `python main.py`
- **Port:** 5000 (required for Replit webview)
- **Auto-reload:** Enabled for development

### Disk Space Management
- Temporary files stored in `./tmp/<uuid>/`
- Files automatically deleted after ZIP is sent to client
- No long-term storage to stay within free tier limits

## Usage Example

### From Google Colab or Python
```python
import requests

response = requests.post(
    "https://your-replit-url.repl.co/api/rumble-zip",
    json={"page_url": "https://rumble.com/user/FusedAegisTV/videos?page=8"}
)

if response.status_code == 200:
    with open("rumble_batch.zip", "wb") as f:
        f.write(response.content)
    print("ZIP file downloaded successfully!")
else:
    print(f"Error: {response.json()}")
```

### From cURL
```bash
curl -X POST https://your-replit-url.repl.co/api/rumble-zip \
  -H "Content-Type: application/json" \
  -d '{"page_url": "https://rumble.com/user/SomeUser/videos"}' \
  --output rumble_batch.zip
```

## Technical Details

### yt-dlp Configuration
- Extract mode: Flat extraction for playlist/page parsing
- Playlist limit: 10 videos maximum
- Format: Best available MP4
- Error handling: Skip failed videos, continue with others
- Partial file cleanup: Failed downloads automatically remove output files and common partial artifacts (.part, .ytdl, .temp, .download) to ensure only successfully downloaded videos are included in the ZIP

### Cleanup Strategy
FastAPI's BackgroundTasks ensures cleanup happens after the response is fully sent to the client. This prevents:
- Premature file deletion
- Disk space exhaustion
- Failed response delivery

### Error Handling
- URL validation with regex pattern matching
- Graceful degradation when individual videos fail
- Clear error messages for troubleshooting
- Proper HTTP status codes for different failure scenarios

### Security & CORS
- API is intentionally credential-free (no authentication or cookies)
- CORS configured with wildcard origins and credentials disabled
- Suitable for public access from Google Colab, scripts, and web applications
- If authentication is added in the future, CORS configuration must be updated to specify allowed origins

## Limitations
- Maximum 10 videos per request (configurable in code)
- Disk space limited to 1GB on Replit free tier
- No concurrent request queuing (processes one at a time)
- Temporary files exist briefly during download/zip process

## Future Enhancements
- Video quality selection (720p, 1080p, best)
- Progress tracking endpoint
- Metadata preservation in ZIP
- Request queuing for concurrent requests
- Disk space monitoring and warnings
