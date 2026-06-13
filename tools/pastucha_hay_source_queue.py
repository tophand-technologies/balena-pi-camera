#!/usr/bin/env python3
"""Build a raw-source image queue for Pastucha Hay labeling.

This intentionally skips TOPHAND branding, but it still verifies capture date
and time from the printed Spypoint overlay before an image becomes labelable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageOps

import tophand_branding_worker as branding


CAMERA_ID = "FLEX-M-MGE4"
CAMERA_TITLE = "Pastucha Hay"
DEFAULT_DATA_DIR = Path("/home/travis/tophand-instances/sdco/research/pastucha-hay")
DEFAULT_RANGES = [
    "jan17-22:2026-01-17:2026-01-22",
    "jan23-30:2026-01-23:2026-01-30",
    "feb15-21:2026-02-15:2026-02-21",
    "mar04-12:2026-03-04:2026-03-12",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Pastucha Hay raw source queue.")
    parser.add_argument("--env", type=Path, default=Path("/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-bucket", default=branding.SOURCE_BUCKET)
    parser.add_argument("--range", action="append", dest="ranges", default=[], help="label:YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--min-bytes", type=int, default=10_000)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--vlm-fallback", action="store_true")
    parser.add_argument("--model", default="qwen3-vl:latest")
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--vlm-timeout", type=int, default=90)
    parser.add_argument("--max-images", type=int)
    parser.add_argument("--sample-minutes", type=int, default=360, help="Keep at most one image per range/time bucket. Use 0 for every image.")
    parser.add_argument(
        "--max-filename-delta-hours",
        type=float,
        default=18.0,
        help="Hold out overlay reads that differ too far from the filename capture hint.",
    )
    return parser.parse_args()


def filename_capture_utc(path: str) -> dt.datetime | None:
    match = re.search(r"_(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", path)
    if not match:
        return None
    year, month, day, hour, minute = map(int, match.groups())
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)
    except ValueError:
        return None


def filename_capture_local(path: str) -> dt.datetime | None:
    captured_utc = filename_capture_utc(path)
    return captured_utc.astimezone(branding.CAPTURE_TZ) if captured_utc else None


def parse_range(raw: str) -> tuple[str, dt.date, dt.date]:
    try:
        label, start, end = raw.split(":", 2)
        start_date = dt.date.fromisoformat(start)
        end_date = dt.date.fromisoformat(end)
    except ValueError as exc:
        raise SystemExit(f"Invalid --range {raw!r}; expected label:YYYY-MM-DD:YYYY-MM-DD") from exc
    if end_date < start_date:
        raise SystemExit(f"Invalid --range {raw!r}; end is before start")
    return label, start_date, end_date


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def overlay_delta_seconds(overlay: dict[str, Any], expected: dt.datetime | None) -> int | None:
    if not expected or not overlay.get("captured_at"):
        return None
    captured = dt.datetime.fromisoformat(overlay["captured_at"])
    return int(abs((captured - expected).total_seconds()))


def overlay_is_usable(overlay: dict[str, Any], expected: dt.datetime | None, max_delta_seconds: float) -> bool:
    source = str(overlay.get("capture_time_source") or "")
    if not (overlay.get("overlay_verified") and overlay.get("captured_at") and source.startswith("image_overlay_")):
        return False
    delta = overlay.get("filename_overlay_delta_seconds")
    if delta is None:
        delta = overlay_delta_seconds(overlay, expected)
    return delta is None or float(delta) <= max_delta_seconds


def attach_filename_cross_check(overlay: dict[str, Any], expected: dt.datetime | None) -> dict[str, Any]:
    if expected:
        overlay["filename_expected_at"] = expected.isoformat()
        overlay["filename_overlay_delta_seconds"] = overlay_delta_seconds(overlay, expected)
    return overlay


def normalize_ocr_text(text: str) -> str:
    clean = text.upper()
    clean = clean.replace("°", " ")
    clean = clean.replace("|", "1")
    clean = re.sub(r"(?<=\d)[Oo](?=\d)", "0", clean)
    clean = re.sub(r"(?<=\D)[Oo](?=\d)", "0", clean)
    clean = re.sub(r"(?<=\d)[Oo](?=\D)", "0", clean)
    return clean


def parse_ocr_overlay(text: str, expected: dt.datetime | None = None) -> tuple[dt.datetime, str, str, str | None]:
    clean = normalize_ocr_text(text)
    date_match = re.search(r"(\d{1,2})\D{1,4}(\d{1,2})\D{1,4}(20\d{2}|2\d{3})", clean)
    if not date_match:
        raise ValueError(f"OCR date not found in: {text[:160]}")
    month, day, year = map(int, date_match.groups())
    if expected and (year < 2020 or year > 2035):
        year = expected.year
    if expected and year != expected.year and month == expected.month and day == expected.day:
        year = expected.year

    time_text = clean[date_match.end() : date_match.end() + 80]
    time_match = re.search(r"([0-2]?\d)\D{0,3}(\d{2})\s*([AP])\s*M?", time_text)
    if not time_match:
        time_match = re.search(r"([0-2]?\d)\s*:\s*(\d{2})\s*([AP])\s*M?", clean)
    if not time_match:
        raise ValueError(f"OCR time not found in: {text[:160]}")
    hour, minute, ampm = time_match.groups()
    hour_int = int(hour)
    minute_int = int(minute)
    if hour_int == 0:
        hour_int = 12
    if not (1 <= hour_int <= 12 and 0 <= minute_int <= 59):
        raise ValueError(f"OCR time invalid in: {text[:160]}")
    if ampm == "P" and hour_int != 12:
        hour_int += 12
    if ampm == "A" and hour_int == 12:
        hour_int = 0

    captured_at = dt.datetime(year, month, day, hour_int, minute_int, tzinfo=branding.CAPTURE_TZ)
    temp_match = re.search(r"(-?\d{1,3})\s*[° ]?\s*([FC])", clean)
    temp_text = f"{int(temp_match.group(1))}{temp_match.group(2)}" if temp_match else None
    return captured_at, f"{month:02d}/{day:02d}/{year}", f"{hour}:{minute} {ampm}M", temp_text


def tesseract_overlay(image: Image.Image, expected: dt.datetime | None, max_delta_seconds: float) -> dict[str, Any]:
    width, height = image.size
    errors: list[str] = []
    for crop_start in (0.955, 0.94, 0.965, 0.925):
        crop = image.crop((0, int(height * crop_start), width, height)).convert("L")
        crop = ImageEnhance.Contrast(crop).enhance(3.0)
        crop = crop.resize((crop.width * 10, crop.height * 10), Image.Resampling.LANCZOS)
        crop = crop.point(lambda value: 255 if value > 135 else 0)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            tmp = Path(handle.name)
            crop.save(tmp)
        try:
            result = subprocess.run(
                ["tesseract", str(tmp), "stdout", "--psm", "7"],
                text=True,
                capture_output=True,
                timeout=20,
            )
        finally:
            tmp.unlink(missing_ok=True)
        raw_text = result.stdout.strip()
        if not raw_text:
            errors.append(f"{crop_start}: empty")
            continue
        try:
            captured_at, date_text, time_text, temp_text = parse_ocr_overlay(raw_text, expected)
            overlay = {
                "overlay_verified": True,
                "capture_time_source": "image_overlay_tesseract",
                "captured_at": captured_at.isoformat(),
                "overlay_date_text": date_text,
                "overlay_time_text": time_text,
                "temperature_text": temp_text,
                "overlay_raw_text": raw_text,
                "overlay_crop_start": crop_start,
            }
            attach_filename_cross_check(overlay, expected)
            if not overlay_is_usable(overlay, expected, max_delta_seconds):
                raise ValueError(
                    "OCR overlay date/time differs from filename hint by "
                    f"{overlay.get('filename_overlay_delta_seconds')} seconds"
                )
            return overlay
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{crop_start}: {exc}")
    raise ValueError("; ".join(errors[:4]))


def vlm_overlay(image: Image.Image, args: argparse.Namespace) -> dict[str, Any]:
    overlay = branding.extract_overlay_data(image, args)
    date_text = branding.clean_text(overlay.get("date_text"))
    time_text = branding.clean_text(overlay.get("time_text"))
    if not date_text or not time_text:
        raise ValueError("VLM response did not include date_text and time_text")
    captured_at = branding.parse_capture_datetime(date_text, time_text)
    temp_text = None
    try:
        temp_text = branding.temperature_display_text(overlay)
    except Exception:  # noqa: BLE001
        pass
    return {
        "overlay_verified": True,
        "capture_time_source": "image_overlay_vlm",
        "captured_at": captured_at.isoformat(),
        "overlay_date_text": date_text,
        "overlay_time_text": time_text,
        "temperature_text": temp_text,
        "overlay_raw_text": branding.clean_text(overlay.get("raw_text")),
        "extractor_model": args.model,
        "extractor_source": overlay.get("_vlm_source"),
        "extractor_seconds": overlay.get("_vlm_seconds"),
    }


def extract_overlay(client: branding.SupabaseRest, source: branding.StorageObject, args: argparse.Namespace) -> dict[str, Any]:
    expected = filename_capture_local(source.path)
    max_delta_seconds = args.max_filename_delta_hours * 3600
    original_bytes = client.download(args.source_bucket, source.path)
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(original_bytes))).convert("RGB")
    try:
        overlay = tesseract_overlay(image, expected, max_delta_seconds)
    except Exception as tesseract_error:
        if not args.vlm_fallback:
            raise
        overlay = vlm_overlay(image, args)
        overlay["tesseract_error"] = str(tesseract_error)
        attach_filename_cross_check(overlay, expected)
        if not overlay_is_usable(overlay, expected, max_delta_seconds):
            raise ValueError(
                "VLM overlay date/time differs from filename hint by "
                f"{overlay.get('filename_overlay_delta_seconds')} seconds"
            )
    return overlay


def main() -> int:
    args = parse_args()
    branding.load_env_file(args.env)
    args.ollama_url = branding.normalize_ollama_url(args.ollama_url or os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST"))
    client = branding.SupabaseRest(
        branding.require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
        branding.require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    )

    ranges = [parse_range(raw) for raw in (args.ranges or DEFAULT_RANGES)]
    source_objects = branding.list_source_objects(client, args.source_bucket, args.limit, args.min_bytes, {CAMERA_ID})
    cache_path = args.cache or (args.data_dir / "source_overlay_cache.json")
    cache = read_json(cache_path, {})
    max_delta_seconds = args.max_filename_delta_hours * 3600
    images: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    processed = 0
    seen_buckets: set[tuple[str, int]] = set()

    for source in source_objects:
        expected_at = filename_capture_local(source.path)
        if not expected_at:
            continue
        expected_range = next((label for label, start, end in ranges if start <= expected_at.date() <= end), None)
        if not expected_range:
            continue
        if args.sample_minutes > 0:
            bucket = int(expected_at.timestamp() // (args.sample_minutes * 60))
            bucket_key = (expected_range, bucket)
            if bucket_key in seen_buckets:
                continue
            seen_buckets.add(bucket_key)

        cached = cache.get(source.path)
        if isinstance(cached, dict) and overlay_is_usable(cached, expected_at, max_delta_seconds):
            overlay = cached
        else:
            if args.max_images is not None and processed >= args.max_images:
                continue
            processed += 1
            try:
                overlay = extract_overlay(client, source, args)
                cache[source.path] = overlay
                write_json(cache_path, cache)
            except Exception as exc:  # noqa: BLE001
                failures.append({"source_path": source.path, "error": str(exc), "expected_at": expected_at.isoformat()})
                cache[source.path] = {"overlay_verified": False, "error": str(exc), "expected_at": expected_at.isoformat()}
                write_json(cache_path, cache)
                continue

        if not overlay_is_usable(overlay, expected_at, max_delta_seconds):
            failures.append(
                {
                    "source_path": source.path,
                    "error": "overlay extraction did not produce a usable image-overlay capture time",
                    "expected_at": expected_at.isoformat(),
                }
            )
            continue

        captured_at = dt.datetime.fromisoformat(overlay["captured_at"])
        for label, start, end in ranges:
            if start <= captured_at.date() <= end:
                images.append(
                    {
                        "path": source.path,
                        "source_path": source.path,
                        "name": source.name,
                        "device": CAMERA_ID,
                        "camera_title": CAMERA_TITLE,
                        "captured_at": captured_at.isoformat(),
                        "capture_time_source": overlay.get("capture_time_source"),
                        "overlay_verified": True,
                        "overlay_date_text": overlay.get("overlay_date_text"),
                        "overlay_time_text": overlay.get("overlay_time_text"),
                        "overlay_raw_text": overlay.get("overlay_raw_text"),
                        "temperature_text": overlay.get("temperature_text"),
                        "filename_expected_at": overlay.get("filename_expected_at"),
                        "filename_overlay_delta_seconds": overlay.get("filename_overlay_delta_seconds"),
                        "created_at": source.created_at,
                        "size": source.size,
                        "queue_range": label,
                    }
                )
                break

    images.sort(key=lambda row: row["captured_at"], reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "source_bucket": args.source_bucket,
        "camera_id": CAMERA_ID,
        "camera_title": CAMERA_TITLE,
        "sample_minutes": args.sample_minutes,
        "ranges": [{"label": label, "start": start.isoformat(), "end": end.isoformat()} for label, start, end in ranges],
        "count": len(images),
        "failed_count": len(failures),
        "failures": failures[:200],
        "images": images,
    }

    output = args.output or (args.data_dir / "source_queue.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(images)} source queue images to {output}")
    print(f"Overlay cache: {cache_path}")
    print(f"Overlay extraction failures: {len(failures)}")
    for item in payload["ranges"]:
        count = sum(1 for image in images if image.get("queue_range") == item["label"])
        print(f"{item['label']}: {item['start']} to {item['end']} = {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
