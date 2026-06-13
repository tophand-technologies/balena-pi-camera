#!/usr/bin/env python3
"""
Trail Camera Capture & Upload Service
For Balena IoT Fleet - Pi Zero with IMX708 Camera
"""

import os
import sys
import time
import subprocess
import logging
from datetime import datetime
from pathlib import Path

# Optional Supabase upload
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

# Configuration from environment
CAPTURE_INTERVAL = int(os.environ.get('CAPTURE_INTERVAL', 300))  # 5 minutes default
IMAGE_WIDTH = os.environ.get('IMAGE_WIDTH', '2304')
IMAGE_HEIGHT = os.environ.get('IMAGE_HEIGHT', '1296')
IMAGE_RESOLUTION = os.environ.get('IMAGE_RESOLUTION', f'{IMAGE_WIDTH}x{IMAGE_HEIGHT}')
DEVICE_NAME = os.environ.get('DEVICE_NAME', os.environ.get('BALENA_DEVICE_NAME_AT_INIT', 'pi-camera'))

# Supabase config
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = (
    os.environ.get('SUPABASE_SECRET_KEY')
    or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    or os.environ.get('SUPABASE_KEY', '')
)
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', 'pi-zero-images')

# Paths
IMAGE_DIR = Path('/data/images')
ARCHIVE_DIR = Path('/data/archive')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def init_supabase() -> Client | None:
    """Initialize Supabase client if credentials are available."""
    if not SUPABASE_AVAILABLE:
        logger.warning("Supabase library not installed, upload disabled")
        return None
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase credentials not configured, upload disabled")
        return None
    
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info(f"Supabase client initialized for bucket: {SUPABASE_BUCKET}")
        return client
    except Exception as e:
        logger.error(f"Failed to initialize Supabase: {e}")
        return None


def capture_image() -> Path | None:
    """Capture an image using rpicam-still."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{DEVICE_NAME}_{timestamp}.jpg"
    filepath = IMAGE_DIR / filename
    
    # Parse resolution
    width, height = IMAGE_RESOLUTION.split('x')
    
    cmd = [
        'rpicam-still',
        '-o', str(filepath),
        '--width', width,
        '--height', height,
        '-t', '2000',  # 2 second warmup
        '--nopreview',
    ]
    
    try:
        logger.info(f"Capturing image: {filename}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logger.error(f"Capture failed: {result.stderr}")
            return None
        
        if filepath.exists():
            size_kb = filepath.stat().st_size / 1024
            logger.info(f"Captured: {filename} ({size_kb:.1f} KB)")
            return filepath
        else:
            logger.error("Capture command succeeded but file not found")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error("Capture timed out")
        return None
    except Exception as e:
        logger.error(f"Capture error: {e}")
        return None


def upload_to_supabase(client: Client, filepath: Path) -> bool:
    """Upload image to Supabase storage."""
    if not client:
        return False
    
    try:
        # Create path in bucket: device_name/YYYY/MM/DD/filename.jpg
        now = datetime.now()
        remote_path = f"{DEVICE_NAME}/{now.year}/{now.month:02d}/{now.day:02d}/{filepath.name}"
        
        with open(filepath, 'rb') as f:
            data = f.read()
        
        logger.info(f"Uploading to: {remote_path}")
        
        response = client.storage.from_(SUPABASE_BUCKET).upload(
            remote_path,
            data,
            file_options={"content-type": "image/jpeg"}
        )
        
        logger.info(f"Upload successful: {filepath.name}")
        return True
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return False


def archive_image(filepath: Path):
    """Move image to archive after successful upload."""
    try:
        archive_path = ARCHIVE_DIR / filepath.name
        filepath.rename(archive_path)
        logger.info(f"Archived: {filepath.name}")
    except Exception as e:
        logger.error(f"Archive failed: {e}")


def cleanup_archive(max_files: int = 100):
    """Keep only the most recent N files in archive."""
    try:
        files = sorted(ARCHIVE_DIR.glob('*.jpg'), key=lambda f: f.stat().st_mtime)
        if len(files) > max_files:
            for f in files[:-max_files]:
                f.unlink()
                logger.info(f"Cleaned up old archive: {f.name}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


def main():
    """Main capture loop."""
    logger.info("=" * 50)
    logger.info("Trail Camera Service Starting")
    logger.info(f"Device: {DEVICE_NAME}")
    logger.info(f"Resolution: {IMAGE_RESOLUTION}")
    logger.info(f"Interval: {CAPTURE_INTERVAL}s")
    logger.info("=" * 50)
    
    # Ensure directories exist
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    
    # Initialize Supabase
    supabase = init_supabase()
    
    # Check camera availability
    logger.info("Checking camera...")
    result = subprocess.run(['rpicam-hello', '--list-cameras'], capture_output=True, text=True)
    if 'imx708' in result.stdout.lower():
        logger.info("IMX708 camera detected")
    else:
        logger.warning("Camera detection output:")
        logger.warning(result.stdout or result.stderr)
    
    # Main loop
    while True:
        try:
            filepath = capture_image()
            
            if filepath:
                if supabase:
                    if upload_to_supabase(supabase, filepath):
                        archive_image(filepath)
                    else:
                        logger.warning(f"Upload failed, keeping local: {filepath.name}")
                else:
                    # No Supabase, just archive locally
                    archive_image(filepath)
                
                cleanup_archive()
            
            logger.info(f"Next capture in {CAPTURE_INTERVAL}s...")
            time.sleep(CAPTURE_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(10)  # Brief pause before retry


if __name__ == '__main__':
    main()
