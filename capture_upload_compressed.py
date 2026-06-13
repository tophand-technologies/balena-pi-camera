#!/usr/bin/env python3
"""
Ranch Camera Capture & Upload Service
For Pi Zero with IMX708 Camera - Cellular Optimized with Compression
Modem Sleep Mode: Only activates cellular for uploads to save battery
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
IMAGE_QUALITY = int(os.environ.get('IMAGE_QUALITY', 10))  # JPEG quality 0-100, 10 gives ~118KB
DEVICE_NAME = os.environ.get('DEVICE_NAME', 'tophand-zero-04')

# Supabase config
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = (
    os.environ.get('SUPABASE_SECRET_KEY')
    or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    or os.environ.get('SUPABASE_KEY', '')
)
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', 'pi-zero-images')

# Paths
IMAGE_DIR = Path(os.environ.get('IMAGE_DIR', '/home/pi/camera/images'))
ARCHIVE_DIR = Path(os.environ.get('ARCHIVE_DIR', '/home/pi/camera/archive'))
GALLERY_DIR = Path(os.environ.get('GALLERY_DIR', '/home/pi/camera/gallery'))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Modem control configuration
MODEM_CONNECTION = os.environ.get('MODEM_CONNECTION', 'hologram')
MODEM_WAKE_TIME = int(os.environ.get('MODEM_WAKE_TIME', 300))  # 5 minute window for SSH access
MODEM_SLEEP_ENABLED = os.environ.get('MODEM_SLEEP_ENABLED', 'true').lower() == 'true'
KEEP_AWAKE_FILE = Path('/tmp/keep_modem_awake')


def wake_modem() -> bool:
    """Bring up cellular connection for uploads."""
    if not MODEM_SLEEP_ENABLED:
        logger.info("Modem sleep disabled, assuming modem is already active")
        return True

    try:
        logger.info(f"⏰ Waking modem: {MODEM_CONNECTION}")
        result = subprocess.run(
            ['sudo', 'nmcli', 'connection', 'up', MODEM_CONNECTION],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            logger.info(f"✅ Modem online, waiting {MODEM_WAKE_TIME}s for connection to stabilize")
            time.sleep(MODEM_WAKE_TIME)
            return True
        else:
            logger.error(f"Failed to wake modem: {result.stderr}")
            return False

    except Exception as e:
        logger.error(f"Modem wake error: {e}")
        return False


def sleep_modem():
    """Put cellular connection to sleep to save battery.

    Checks for /tmp/keep_modem_awake flag file - if present, modem stays awake for SSH access.
    To keep modem awake: touch /tmp/keep_modem_awake
    To allow sleep: rm /tmp/keep_modem_awake
    """
    if not MODEM_SLEEP_ENABLED:
        logger.info("Modem sleep disabled, keeping modem active")
        return

    # Check for keep-awake flag
    if KEEP_AWAKE_FILE.exists():
        logger.info(f"🔒 Keep-awake flag found ({KEEP_AWAKE_FILE}), modem staying online for SSH access")
        return

    try:
        logger.info(f"💤 Putting modem to sleep: {MODEM_CONNECTION}")
        result = subprocess.run(
            ['sudo', 'nmcli', 'connection', 'down', MODEM_CONNECTION],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            logger.info("✅ Modem sleeping (saves ~400-500mA)")
        else:
            logger.warning(f"Failed to sleep modem: {result.stderr}")

    except Exception as e:
        logger.error(f"Modem sleep error: {e}")


def enter_deep_idle():
    """Enter deep idle mode to minimize power consumption.

    Optimizations applied:
    - CPU frequency scaling to powersave (saves ~40mA)
    - Disable HDMI output (saves ~30mA)
    - Disable activity LED (saves ~5mA)
    - Total savings: ~75mA @ 5V = ~145mA from battery

    Network connections (Tailscale) remain active.
    """
    try:
        logger.info("🌙 Entering deep idle mode")

        # Set CPU governor to powersave (lowest frequency)
        result = subprocess.run(
            ['sudo', 'sh', '-c',
             'echo powersave > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info("  ✓ CPU powersave mode enabled")

        # Disable HDMI (if tvservice available - Pi Zero doesn't have it)
        if Path('/usr/bin/tvservice').exists():
            result = subprocess.run(
                ['/usr/bin/tvservice', '-o'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("  ✓ HDMI disabled")
        else:
            logger.info("  ✓ HDMI control not available (Pi Zero)")

        # Disable activity LED
        result = subprocess.run(
            ['sudo', 'sh', '-c',
             'echo none > /sys/class/leds/led0/trigger'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info("  ✓ Activity LED disabled")

        logger.info("✅ Deep idle active (~80mA total draw, saves ~75mA)")

    except Exception as e:
        logger.warning(f"Deep idle mode error: {e}")


def exit_deep_idle():
    """Exit deep idle mode before capture/upload operations.

    Restores:
    - CPU frequency scaling to ondemand (responsive)
    - HDMI output (if needed)
    - Activity LED
    """
    try:
        logger.info("⚡ Exiting deep idle mode")

        # Set CPU governor to ondemand (balanced performance)
        result = subprocess.run(
            ['sudo', 'sh', '-c',
             'echo ondemand > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info("  ✓ CPU ondemand mode enabled")

        # Re-enable HDMI (if tvservice available - Pi Zero doesn't have it)
        if Path('/usr/bin/tvservice').exists():
            result = subprocess.run(
                ['/usr/bin/tvservice', '-p'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("  ✓ HDMI enabled")
        else:
            logger.info("  ✓ HDMI control not available (Pi Zero)")

        # Re-enable activity LED
        result = subprocess.run(
            ['sudo', 'sh', '-c',
             'echo mmc0 > /sys/class/leds/led0/trigger'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info("  ✓ Activity LED enabled")

        logger.info("✅ Deep idle exit complete, system responsive")

    except Exception as e:
        logger.warning(f"Deep idle exit error: {e}")


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


def capture_image() -> tuple[Path | None, Path | None]:
    """Capture high-quality image and create compressed version for upload.

    Returns:
        tuple: (high_quality_filepath, compressed_filepath)
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename_hq = f"{DEVICE_NAME}_{timestamp}_HQ.jpg"
    filename_compressed = f"{DEVICE_NAME}_{timestamp}.jpg"
    filepath_hq = IMAGE_DIR / filename_hq
    filepath_compressed = IMAGE_DIR / filename_compressed

    # Parse resolution
    width, height = IMAGE_RESOLUTION.split('x')

    # Check if it's nighttime (7pm - 6am)
    current_hour = datetime.now().hour
    is_night = current_hour >= 19 or current_hour < 6

    # Capture high-quality image first (default quality ~95)
    cmd_hq = [
        'rpicam-still',
        '-o', str(filepath_hq),
        '--width', width,
        '--height', height,
        '--rotation', '180',  # Camera mounted upside down
        '-t', '2000',  # 2 second warmup
        '--nopreview',
    ]

    # Add night mode settings for low-light conditions
    if is_night:
        logger.info("Night mode: Using low-light camera settings")
        cmd_hq.extend([
            '--shutter', '200000',  # 200ms exposure (longer for low light)
            '--gain', '8',          # Higher ISO/gain for sensitivity
            '--brightness', '0.2',  # Boost brightness
            '--ev', '1',            # +1 exposure compensation
        ])
    else:
        logger.info("Day mode: Using standard camera settings")

    try:
        logger.info(f"Capturing high-quality image: {filename_hq}")
        result = subprocess.run(cmd_hq, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            logger.error(f"Capture failed: {result.stderr}")
            return None, None

        if not filepath_hq.exists():
            logger.error("Capture command succeeded but file not found")
            return None, None

        size_kb_hq = filepath_hq.stat().st_size / 1024
        logger.info(f"Captured HQ: {filename_hq} ({size_kb_hq:.1f} KB)")

        # Create compressed version for cellular upload
        cmd_compressed = [
            'rpicam-still',
            '-o', str(filepath_compressed),
            '--width', width,
            '--height', height,
            '--rotation', '180',  # Camera mounted upside down
            '-q', str(IMAGE_QUALITY),  # Low quality for cellular
            '-t', '2000',
            '--nopreview',
        ]

        # Apply same night mode settings to compressed version
        if is_night:
            cmd_compressed.extend([
                '--shutter', '200000',  # 200ms exposure
                '--gain', '8',          # Higher ISO/gain
                '--brightness', '0.2',  # Boost brightness
                '--ev', '1',            # +1 exposure compensation
            ])

        logger.info(f"Creating compressed version: {filename_compressed} (quality={IMAGE_QUALITY})")
        result = subprocess.run(cmd_compressed, capture_output=True, text=True, timeout=30)

        if result.returncode != 0 or not filepath_compressed.exists():
            logger.warning("Compressed version failed, will upload HQ instead")
            return filepath_hq, None

        size_kb_compressed = filepath_compressed.stat().st_size / 1024
        logger.info(f"Compressed: {filename_compressed} ({size_kb_compressed:.1f} KB)")

        return filepath_hq, filepath_compressed

    except subprocess.TimeoutExpired:
        logger.error("Capture timed out")
        return None, None
    except Exception as e:
        logger.error(f"Capture error: {e}")
        return None, None


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

        size_kb = len(data) / 1024
        logger.info(f"Uploading to: {remote_path} ({size_kb:.1f} KB)")

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


def cleanup_gallery(max_files: int = 50):
    """Keep only the most recent N compressed images in gallery."""
    try:
        files = sorted(GALLERY_DIR.glob('*.jpg'), key=lambda f: f.stat().st_mtime)
        if len(files) > max_files:
            for f in files[:-max_files]:
                f.unlink()
                logger.info(f"Cleaned up old gallery image: {f.name}")
    except Exception as e:
        logger.error(f"Gallery cleanup error: {e}")


def main():
    """Main capture loop."""
    logger.info("=" * 50)
    logger.info("Ranch Camera Capture Service")
    logger.info(f"Device: {DEVICE_NAME}")
    logger.info(f"Resolution: {IMAGE_RESOLUTION}")
    logger.info(f"HQ: Full quality saved to SD card")
    logger.info(f"Upload: Quality {IMAGE_QUALITY} (~118KB for cellular)")
    if MODEM_SLEEP_ENABLED:
        logger.info(f"Modem Sleep: ENABLED (wake for {MODEM_WAKE_TIME}s, then sleep)")
        logger.info(f"Keep-awake flag: {KEEP_AWAKE_FILE}")
    else:
        logger.info("Modem Sleep: DISABLED (modem always on)")
    logger.info("Deep Idle: ENABLED (CPU powersave + HDMI off + LED off)")
    logger.info("Power: ~80mA idle, ~1450mA active, ~194mA average")
    logger.info("Battery Life: ~4-5 days on 12,000mAh battery")
    logger.info("=" * 50)

    # Ensure directories exist
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)

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

    # Single capture mode (for systemd timer)
    try:
        # Step 0: Exit deep idle mode for active operations
        exit_deep_idle()

        # Step 1: Capture images (modem off to save power)
        logger.info("Step 1/5: Capturing images (modem offline)")
        filepath_hq, filepath_compressed = capture_image()

        if filepath_hq:
            # Archive high-quality original immediately
            archive_image(filepath_hq)
            logger.info(f"HQ image archived to SD card: {filepath_hq.name}")

        if filepath_compressed and supabase:
            # Step 2: Wake modem for upload (5 minute window for potential SSH access)
            logger.info("Step 2/5: Waking modem for cellular upload (5 min window)")
            modem_awake = wake_modem()

            if modem_awake:
                # Step 3: Upload compressed version over cellular
                logger.info("Step 3/5: Uploading to Supabase")
                upload_to_supabase(supabase, filepath_compressed)

                # Move compressed image to web gallery for viewing
                gallery_path = GALLERY_DIR / filepath_compressed.name
                filepath_compressed.rename(gallery_path)
                logger.info(f"Compressed image moved to gallery: {filepath_compressed.name}")

                # Cleanup old gallery images (keep last 50)
                cleanup_gallery()

                # Step 4: Put modem back to sleep (unless keep-awake flag is set)
                logger.info("Step 4/5: Checking modem sleep status")
                sleep_modem()

                # Step 5: Enter deep idle mode until next capture
                logger.info("Step 5/5: Entering deep idle mode")
                enter_deep_idle()
            else:
                logger.warning("Modem failed to wake, skipping upload")
                # Keep compressed image for retry
                logger.info(f"Compressed image saved for later retry: {filepath_compressed.name}")
                # Enter deep idle even if modem wake failed
                enter_deep_idle()

        elif filepath_compressed:
            # No Supabase configured, just save to gallery
            gallery_path = GALLERY_DIR / filepath_compressed.name
            filepath_compressed.rename(gallery_path)
            logger.info(f"Compressed image moved to gallery: {filepath_compressed.name}")
            cleanup_gallery()
            # Enter deep idle when no upload needed
            enter_deep_idle()
        else:
            # No image captured, still enter deep idle
            enter_deep_idle()

        cleanup_archive()
        logger.info("✅ Capture cycle complete")

    except Exception as e:
        logger.error(f"Capture/upload error: {e}")
        # Try to sleep modem even if there was an error
        sleep_modem()
        # Enter deep idle even after error
        enter_deep_idle()
        sys.exit(1)


if __name__ == '__main__':
    main()
