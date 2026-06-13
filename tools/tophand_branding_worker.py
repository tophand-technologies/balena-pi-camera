#!/usr/bin/env python3
"""Create TOPHAND-branded versions of Spypoint ranch images.

The printed bottom overlay on the original image is treated as the source of
truth for capture date, capture time, temperature, and camera identity.
Storage timestamps and filename timestamps are only used to build the queue.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - argparse help should still work
    requests = None  # type: ignore[assignment]


SOURCE_BUCKET = "spypoint-images"
DEST_BUCKET = "tophand-branded-images"
IMAGE_TABLE = "spypoint_images"
CAPTURE_TZ = ZoneInfo("America/Chicago")
DEST_BUCKET_MIME_TYPES = ["image/jpeg", "application/json"]

CAMERA_NAMES = {
    "FLEX-M-MGE4": "Pastucha Hay",
    "FLEX-M-NGEF": "Back Yard",
    "FLEX-M-RJQM": "Ainsworth Gate",
    "FLEX-S-DARK-RJQH": "Cattle Pen",
    "QC": "Pastucha Pond",
    "QN": "Donna Trough 1",
    "YV": "Donna Trough 2",
    "tophand-zero-04": "ZeroCam 04",
}

EXCLUDED_ROOTS = {"Untitled folder", "test"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class StorageObject:
    path: str
    name: str
    device: str
    created_at: str | None
    size: int


class WorkerError(RuntimeError):
    pass


def load_env_file(path: Path | None) -> None:
    if not path or not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str, *fallbacks: str) -> str:
    value = os.environ.get(name)
    for fallback in fallbacks:
        value = value or os.environ.get(fallback)
    if not value:
        names = ", ".join((name, *fallbacks))
        raise WorkerError(f"Missing required environment variable: one of {names}")
    return value.rstrip("/")


def normalize_ollama_url(raw: str | None) -> str:
    value = (raw or "http://127.0.0.1:11434").rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value


def api_json(response: Any) -> Any:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text[:500]
        raise WorkerError(f"HTTP {response.status_code}: {detail}") from exc
    if not response.content:
        return None
    return response.json()


class SupabaseRest:
    def __init__(self, url: str, key: str, timeout: int = 90) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.image_table_missing = False

    def headers(self, content_type: str | None = "application/json", prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def list_buckets(self) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.url}/storage/v1/bucket",
            headers=self.headers(content_type=None),
            timeout=self.timeout,
        )
        return api_json(response)

    def ensure_public_bucket(self, bucket: str) -> None:
        existing = {item.get("name") or item.get("id") for item in self.list_buckets()}
        if bucket in existing:
            response = requests.put(
                f"{self.url}/storage/v1/bucket/{bucket}",
                headers=self.headers(),
                json={
                    "public": True,
                    "allowed_mime_types": DEST_BUCKET_MIME_TYPES,
                },
                timeout=self.timeout,
            )
            api_json(response)
            return

        response = requests.post(
            f"{self.url}/storage/v1/bucket",
            headers=self.headers(),
            json={
                "id": bucket,
                "name": bucket,
                "public": True,
                "allowed_mime_types": DEST_BUCKET_MIME_TYPES,
            },
            timeout=self.timeout,
        )
        if response.status_code not in {200, 201, 409}:
            api_json(response)

    def list_folder(self, bucket: str, prefix: str, limit: int = 1000) -> list[dict[str, Any]]:
        offset = 0
        rows: list[dict[str, Any]] = []
        while True:
            response = None
            for attempt in range(3):
                response = requests.post(
                    f"{self.url}/storage/v1/object/list/{bucket}",
                    headers=self.headers(),
                    json={
                        "prefix": prefix,
                        "limit": limit,
                        "offset": offset,
                        "sortBy": {"column": "name", "order": "asc"},
                    },
                    timeout=self.timeout,
                )
                if response.status_code not in {502, 503, 504}:
                    break
                time.sleep(2 * (attempt + 1))
            if response is None:
                raise WorkerError(f"Could not list {bucket}/{prefix}")
            batch = api_json(response)
            if not batch:
                return rows
            rows.extend(batch)
            if len(batch) < limit:
                return rows
            offset += limit

    def download(self, bucket: str, path: str) -> bytes:
        response = requests.get(
            f"{self.url}/storage/v1/object/{bucket}/{quote(path, safe='/')}",
            headers=self.headers(content_type=None),
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise WorkerError(f"Failed downloading {path}: HTTP {response.status_code}") from exc
        return response.content

    def download_json_optional(self, bucket: str, path: str) -> dict[str, Any] | None:
        response = None
        for attempt in range(3):
            response = requests.get(
                f"{self.url}/storage/v1/object/{bucket}/{quote(path, safe='/')}",
                headers=self.headers(content_type=None),
                timeout=self.timeout,
            )
            if response.status_code not in {502, 503, 504}:
                break
            time.sleep(2 * (attempt + 1))
        if response is None:
            return None
        if response.status_code == 404:
            return None
        if response.status_code == 400 and '"statusCode":"404"' in response.text:
            return None
        if response.status_code in {502, 503, 504}:
            return None
        try:
            payload = api_json(response)
        except WorkerError:
            return None
        return payload if isinstance(payload, dict) else None

    def object_exists(self, bucket: str, path: str) -> bool:
        response = requests.head(
            f"{self.url}/storage/v1/object/{bucket}/{quote(path, safe='/')}",
            headers=self.headers(content_type=None),
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return False
        if response.status_code in {200, 304}:
            return True
        response.raise_for_status()
        return False

    def upload_bytes(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        response = None
        for attempt in range(3):
            response = requests.post(
                f"{self.url}/storage/v1/object/{bucket}/{quote(path, safe='/')}",
                headers={
                    **self.headers(content_type=content_type),
                    "Cache-Control": "3600",
                    "x-upsert": "true",
                },
                data=data,
                timeout=self.timeout,
            )
            if response.status_code not in {502, 503, 504}:
                break
            time.sleep(2 * (attempt + 1))
        if response is None:
            raise WorkerError(f"Failed uploading {path}: no response")
        if response.status_code not in {200, 201}:
            detail = response.text[:500]
            raise WorkerError(f"Failed uploading {path}: HTTP {response.status_code}: {detail}")

    def upload_jpeg(self, bucket: str, path: str, data: bytes) -> None:
        self.upload_bytes(bucket, path, data, "image/jpeg")

    def public_url(self, bucket: str, path: str) -> str:
        return f"{self.url}/storage/v1/object/public/{bucket}/{quote(path, safe='/')}"

    def select_image_record(self, storage_path: str) -> dict[str, Any] | None:
        if self.image_table_missing:
            return None
        query_path = quote(storage_path, safe="")
        response = requests.get(
            f"{self.url}/rest/v1/{IMAGE_TABLE}?storage_path=eq.{query_path}&select=*",
            headers=self.headers(content_type=None),
            timeout=self.timeout,
        )
        if response.status_code == 404 and "PGRST205" in response.text:
            self.image_table_missing = True
            return None
        rows = api_json(response)
        return rows[0] if rows else None

    def update_image_record(self, storage_path: str, patch: dict[str, Any]) -> bool:
        if self.image_table_missing:
            return False
        query_path = quote(storage_path, safe="")
        response = requests.patch(
            f"{self.url}/rest/v1/{IMAGE_TABLE}?storage_path=eq.{query_path}",
            headers=self.headers(prefer="return=representation"),
            json=patch,
            timeout=self.timeout,
        )
        if response.status_code == 404 and "PGRST205" in response.text:
            self.image_table_missing = True
            return False
        rows = api_json(response)
        return bool(rows)

    def upsert_image_record(self, record: dict[str, Any]) -> bool:
        if self.image_table_missing:
            return False
        response = requests.post(
            f"{self.url}/rest/v1/{IMAGE_TABLE}?on_conflict=image_id",
            headers=self.headers(prefer="resolution=merge-duplicates,return=representation"),
            json=record,
            timeout=self.timeout,
        )
        if response.status_code == 404 and "PGRST205" in response.text:
            self.image_table_missing = True
            return False
        rows = api_json(response)
        return bool(rows)


def is_folder(item: dict[str, Any]) -> bool:
    return item.get("id") is None


def parse_sort_time(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def is_real_source_image(path: str, size: int, min_bytes: int) -> bool:
    lower = path.lower()
    suffix = Path(path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return False
    if size and size < min_bytes:
        return False
    return not (
        "_thumb" in lower
        or "thumb." in lower
        or "/thumb/" in lower
        or "/thumbnails/" in lower
        or "/hd/" in lower
    )


def list_source_objects(
    client: SupabaseRest,
    bucket: str,
    limit: int,
    min_bytes: int,
    camera_filter: set[str] | None,
) -> list[StorageObject]:
    objects: list[StorageObject] = []

    def walk(prefix: str, device: str) -> None:
        for item in client.list_folder(bucket, prefix):
            name = item.get("name", "")
            if not name:
                continue

            full_path = f"{prefix}/{name}" if prefix else name
            if is_folder(item):
                folder = name.lower()
                if "thumb" in folder or folder == "hd":
                    continue
                walk(full_path, device)
                continue

            size = int((item.get("metadata") or {}).get("size") or 0)
            if not is_real_source_image(full_path, size, min_bytes):
                continue
            if camera_filter and device not in camera_filter:
                continue

            objects.append(
                StorageObject(
                    path=full_path,
                    name=name,
                    device=device,
                    created_at=item.get("created_at") or item.get("updated_at"),
                    size=size,
                )
            )

    for item in client.list_folder(bucket, ""):
        name = item.get("name", "")
        if not name or name in EXCLUDED_ROOTS:
            continue
        if is_folder(item):
            if camera_filter and name not in camera_filter:
                continue
            walk(name, name)
        elif is_real_source_image(name, int((item.get("metadata") or {}).get("size") or 0), min_bytes):
            device = name.split("/", 1)[0]
            if not camera_filter or device in camera_filter:
                objects.append(
                    StorageObject(
                        path=name,
                        name=name,
                        device=device,
                        created_at=item.get("created_at") or item.get("updated_at"),
                        size=int((item.get("metadata") or {}).get("size") or 0),
                    )
                )

    objects.sort(key=lambda item: parse_sort_time(item.created_at), reverse=True)
    return objects[:limit]


def source_paths_to_objects(paths: list[str]) -> list[StorageObject]:
    objects = []
    for path in paths:
        clean_path = path.strip().strip("/")
        if not clean_path:
            continue
        objects.append(
            StorageObject(
                path=clean_path,
                name=Path(clean_path).name,
                device=clean_path.split("/", 1)[0],
                created_at=None,
                size=0,
            )
        )
    return objects


def parse_branded_capture_time(name: str, fallback: str | None) -> str:
    match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", name)
    if match:
        year, month, day, hour, minute = match.groups()
        captured = dt.datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            tzinfo=CAPTURE_TZ,
        )
        return captured.isoformat()
    return parse_sort_time(fallback).astimezone(CAPTURE_TZ).isoformat()


def branded_metadata_path(branded_path: str) -> str:
    return f"_metadata/{branded_path}.json"


def publish_manifest(client: SupabaseRest, bucket: str, limit: int) -> int:
    objects = list_source_objects(
        client=client,
        bucket=bucket,
        limit=limit,
        min_bytes=1,
        camera_filter=None,
    )
    entries = []
    for item in objects:
        metadata = client.download_json_optional(bucket, branded_metadata_path(item.path)) or {}
        entries.append(
            {
                "name": item.name,
                "path": item.path,
                "device": item.device,
                "captured_at": parse_branded_capture_time(item.name, item.created_at),
                "camera_title": CAMERA_NAMES.get(item.device, item.device),
                "temperature_text": metadata.get("overlay_temperature_text"),
                "source_path": metadata.get("source_storage_path"),
                "vlm": {
                    "summary": metadata.get("overlay_raw_text"),
                    "extractor_model": metadata.get("extractor_model"),
                    "extractor_source": metadata.get("extractor_source"),
                    "extractor_seconds": metadata.get("extractor_seconds"),
                }
                if metadata
                else None,
                "analysis": metadata.get("analysis") or metadata.get("ranch_eye_analysis"),
            }
        )
    entries.sort(key=lambda row: parse_sort_time(row["captured_at"]), reverse=True)
    manifest = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "bucket": bucket,
        "count": len(entries),
        "images": entries,
    }
    payload = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
    client.upload_bytes(bucket, "manifest.json", payload, "application/json")
    return len(entries)


def manifest_source_paths(client: SupabaseRest, bucket: str) -> set[str]:
    manifest = client.download_json_optional(bucket, "manifest.json") or {}
    paths = set()
    for item in manifest.get("images") or []:
        if not isinstance(item, dict):
            continue
        source_path = clean_text(item.get("source_path"))
        if source_path:
            paths.add(source_path)
    return paths


def find_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, bold: bool = False) -> ImageFont.ImageFont:
    for size in range(start, 8, -1):
        font = find_font(size, bold=bold)
        width, _ = text_size(draw, text, font)
        if width <= max_width:
            return font
    return find_font(8, bold=bold)


def image_to_jpeg_bytes(image: Image.Image, quality: int = 92) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def overlay_crop_bytes(image: Image.Image) -> bytes:
    width, height = image.size
    crop_top = max(0, round(height * 0.74))
    crop = image.crop((0, crop_top, width, height)).convert("RGB")
    if crop.width < 2600:
        scale = 2600 / crop.width
        crop = crop.resize((2600, max(1, round(crop.height * scale))), Image.Resampling.LANCZOS)
    return image_to_jpeg_bytes(crop, quality=94)


def draw_tophand_overlay(
    image: Image.Image,
    date_text: str,
    time_text: str,
    temp_text: str,
    camera_title: str,
) -> bytes:
    canvas = ImageOps.exif_transpose(image).convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size

    bar_h = max(40, round(height * 0.105))
    y0 = height - bar_h
    bg = "#050505"
    accent = "#d6b56d"
    label = "#ffffff"

    draw.rectangle((0, y0, width, height), fill=bg)
    draw.rectangle((0, y0, width, y0 + 3), fill=accent)

    pad = max(14, round(width * 0.025))
    center_x = width // 2
    mid_y = y0 + bar_h // 2
    center_w = round(width * 0.26)
    side_w = max(120, (width - pad * 2 - center_w) // 2)

    left_text = f"{date_text} | {time_text} | {temp_text}"
    right_text = camera_title.upper()

    side_start = max(15, round(bar_h * 0.38))
    left_font = fit_font(draw, left_text, side_w, side_start, bold=True)
    right_font = fit_font(draw, right_text, side_w, side_start, bold=True)
    brand_font = fit_font(draw, "TOPHAND", center_w, max(24, round(bar_h * 0.6)), bold=True)

    draw.text((pad, mid_y), left_text, font=left_font, fill=label, anchor="lm")
    draw.text((width - pad, mid_y), right_text, font=right_font, fill=label, anchor="rm")
    draw.text((center_x, mid_y), "TOPHAND", font=brand_font, fill=accent, anchor="mm")

    return image_to_jpeg_bytes(canvas, quality=92)


def call_ollama_vlm(ollama_url: str, model: str, image_bytes: bytes, timeout: int) -> str:
    prompt = (
        "Read only the printed black status bar along the bottom edge of this trail camera image. "
        "Do not use filename, file metadata, upload time, or your assumptions. Return strict JSON only, "
        "with these keys: date_text, time_text, temperature_text, temperature_f, camera_id, raw_text. "
        "temperature_text must preserve the printed unit exactly when visible, such as 87F or 26C. "
        "Use null for any field that is genuinely unreadable. Include AM or PM in time_text if printed."
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0},
    }
    response = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout)
    data = api_json(response)
    return (data or {}).get("response", "")


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise WorkerError(f"VLM did not return JSON: {cleaned[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise WorkerError("VLM JSON response was not an object")
    return parsed


def extract_overlay_data(image: Image.Image, args: argparse.Namespace) -> dict[str, Any]:
    attempts = [
        ("bottom_crop", overlay_crop_bytes(image)),
        ("full_image", image_to_jpeg_bytes(ImageOps.exif_transpose(image).convert("RGB"), quality=90)),
    ]
    last_error: Exception | None = None
    for source, jpeg_bytes in attempts:
        started = time.time()
        try:
            response_text = call_ollama_vlm(args.ollama_url, args.model, jpeg_bytes, args.vlm_timeout)
            data = extract_json_object(response_text)
            data["_vlm_source"] = source
            data["_vlm_seconds"] = round(time.time() - started, 2)
            if clean_text(data.get("date_text")) and clean_text(data.get("time_text")):
                return data
        except Exception as exc:  # noqa: BLE001 - preserve retry path details in report
            last_error = exc
    raise WorkerError(f"Could not extract overlay data: {last_error}")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "unknown", "unreadable"}:
        return None
    return re.sub(r"\s+", " ", text)


def temperature_display_text(data: dict[str, Any]) -> str:
    candidates = [data.get("temperature_text"), data.get("temperature"), data.get("temp"), data.get("raw_text")]
    for value in candidates:
        if value is None:
            continue
        match = re.search(r"(-?\d{1,3})\s*(?:°|\s)*(?:deg|degrees)?\s*([FC])", str(value), flags=re.IGNORECASE)
        if match:
            return f"{int(match.group(1))}{match.group(2).upper()}"

    fallback = data.get("temperature_f")
    if isinstance(fallback, (int, float)):
        return f"{int(round(float(fallback)))}F"
    if fallback is not None:
        match = re.search(r"(-?\d{1,3})", str(fallback))
        if match:
            return f"{int(match.group(1))}F"
    raise WorkerError("VLM did not return a readable temperature")


def temperature_value(temp_text: str) -> int | None:
    match = re.search(r"-?\d{1,3}", temp_text)
    return int(match.group(0)) if match else None


def parse_capture_datetime(date_text: str, time_text: str) -> dt.datetime:
    date_clean = date_text.strip().replace(".", "/").replace("-", "/")
    time_clean = time_text.strip().upper().replace(".", "")
    time_clean = re.sub(r"\s+", " ", time_clean)
    time_clean = re.sub(r"([0-9])([AP]M)$", r"\1 \2", time_clean)
    time_clean = re.sub(r"^00(:\d{2}(?::\d{2})?\s+[AP]M)$", r"12\1", time_clean)

    date_formats = ["%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%Y/%d/%m"]
    time_formats = ["%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"]

    parsed_date: dt.date | None = None
    parsed_time: dt.time | None = None
    for fmt in date_formats:
        try:
            parsed_date = dt.datetime.strptime(date_clean, fmt).date()
            break
        except ValueError:
            continue
    for fmt in time_formats:
        try:
            parsed_time = dt.datetime.strptime(time_clean, fmt).time()
            break
        except ValueError:
            continue

    if parsed_date is None or parsed_time is None:
        raise WorkerError(f"Could not parse overlay date/time: {date_text!r} {time_text!r}")

    return dt.datetime.combine(parsed_date, parsed_time, tzinfo=CAPTURE_TZ)


def display_date(capture_at: dt.datetime) -> str:
    return capture_at.strftime("%m/%d/%y")


def display_time(capture_at: dt.datetime) -> str:
    return capture_at.strftime("%I:%M %p").lstrip("0")


def camera_title(device: str, row: dict[str, Any] | None, extracted_camera_id: str | None) -> str:
    if device in CAMERA_NAMES:
        return CAMERA_NAMES[device]
    if row and clean_text(row.get("camera_name")):
        return clean_text(row.get("camera_name")) or device
    return clean_text(extracted_camera_id) or device


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "camera"


def build_destination_path(source: StorageObject, capture_at: dt.datetime, title: str) -> str:
    source_stem = Path(source.name).stem
    stamped = capture_at.strftime("%Y%m%d_%H%M")
    return (
        f"{source.device}/{capture_at:%Y/%m/%d}/"
        f"{stamped}_{slugify(title)}_{source_stem}_tophand.jpg"
    )


def source_image_id(source: StorageObject) -> str:
    stem = Path(source.name).stem
    return re.sub(r"_(?:rancheye|tophand)$", "", stem, flags=re.IGNORECASE)


def merge_metadata(row: dict[str, Any] | None, version: dict[str, Any]) -> dict[str, Any]:
    current = (row or {}).get("metadata") or {}
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            current = {"previous_raw_value": current}
    if not isinstance(current, dict):
        current = {}
    current["tophand_branding_v1"] = version
    current["capture_time_source"] = "image_overlay"
    return current


def merge_overlay_versions(row: dict[str, Any] | None, version_name: str) -> list[str]:
    current = (row or {}).get("overlay_versions") or []
    if isinstance(current, str):
        if current in {"{}", ""}:
            current = []
        else:
            current = [part.strip().strip('"') for part in current.strip("{}").split(",") if part.strip()]
    if not isinstance(current, list):
        current = []
    versions = [str(item) for item in current]
    if version_name not in versions:
        versions.append(version_name)
    return versions


def write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def process_one(client: SupabaseRest, source: StorageObject, args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "source_path": source.path,
        "source_bucket": args.source_bucket,
        "status": "started",
    }

    original_bytes = client.download(args.source_bucket, source.path)
    image = Image.open(io.BytesIO(original_bytes))
    image = ImageOps.exif_transpose(image)

    overlay = extract_overlay_data(image, args)
    date_text = clean_text(overlay.get("date_text"))
    time_text = clean_text(overlay.get("time_text"))
    if not date_text or not time_text:
        raise WorkerError("VLM response did not include date_text and time_text")

    capture_at = parse_capture_datetime(date_text, time_text)
    temp_text = temperature_display_text(overlay)
    temp_value = temperature_value(temp_text)
    row = client.select_image_record(source.path)
    extracted_camera_id = clean_text(overlay.get("camera_id"))
    title = camera_title(source.device, row, extracted_camera_id)

    branded_bytes = draw_tophand_overlay(
        image=image,
        date_text=display_date(capture_at),
        time_text=display_time(capture_at),
        temp_text=temp_text,
        camera_title=title,
    )
    destination_path = build_destination_path(source, capture_at, title)

    if args.save_local_dir:
        local_path = args.save_local_dir / destination_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(branded_bytes)
        report["local_path"] = str(local_path)

    version = {
        "style": "tophand_a1",
        "source_bucket": args.source_bucket,
        "source_storage_path": source.path,
        "branded_bucket": args.dest_bucket,
        "branded_storage_path": destination_path,
        "branded_public_url": client.public_url(args.dest_bucket, destination_path),
        "overlay_capture_at": capture_at.isoformat(),
        "overlay_temperature_text": temp_text,
        "overlay_temperature_value": temp_value,
        "overlay_camera_id": extracted_camera_id,
        "overlay_camera_title": title,
        "overlay_raw_text": clean_text(overlay.get("raw_text")),
        "overlay_date_text": date_text,
        "overlay_time_text": time_text,
        "extractor_model": args.model,
        "extractor_source": overlay.get("_vlm_source"),
        "extractor_seconds": overlay.get("_vlm_seconds"),
        "branded_at": dt.datetime.now(dt.UTC).isoformat(),
    }

    report.update(
        {
            "status": "dry_run",
            "destination_path": destination_path,
            "capture_at": capture_at.isoformat(),
            "temperature_text": temp_text,
            "camera_title": title,
            "vlm_source": overlay.get("_vlm_source"),
            "vlm_seconds": overlay.get("_vlm_seconds"),
            "db_record_found": bool(row),
        }
    )

    if args.write:
        client.upload_jpeg(args.dest_bucket, destination_path, branded_bytes)
        client.upload_bytes(
            args.dest_bucket,
            branded_metadata_path(destination_path),
            json.dumps(version, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            "application/json",
        )
        db_updated = False
        if args.update_db:
            metadata = merge_metadata(row, version)
            overlay_versions = merge_overlay_versions(row, "tophand_a1")
            patch = {
                "captured_at": capture_at.isoformat(),
                "metadata": metadata,
                "overlay_versions": overlay_versions,
            }
            if row:
                db_updated = client.update_image_record(source.path, patch)
            elif args.insert_missing_db_records:
                db_updated = client.upsert_image_record(
                    {
                        "image_id": source_image_id(source),
                        "camera_name": source.device,
                        "storage_path": source.path,
                        "image_url": client.public_url(args.source_bucket, source.path),
                        "captured_at": capture_at.isoformat(),
                        "metadata": metadata,
                        "overlay_versions": overlay_versions,
                    }
                )
        report["status"] = "uploaded"
        report["db_updated"] = db_updated
        report["metadata_path"] = branded_metadata_path(destination_path)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate TOPHAND-branded Supabase image copies.")
    parser.add_argument("--env", type=Path, default=Path("/home/travis/rancheye-unified/.env"))
    parser.add_argument("--source-bucket", default=SOURCE_BUCKET)
    parser.add_argument("--dest-bucket", default=DEST_BUCKET)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--camera", action="append", help="Process only this source camera folder. May be repeated.")
    parser.add_argument("--source-path", action="append", help="Process this exact storage path. May be repeated.")
    parser.add_argument("--min-bytes", type=int, default=10_000)
    parser.add_argument("--model", default="qwen3-vl:latest")
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--vlm-timeout", type=int, default=180)
    parser.add_argument("--write", action="store_true", help="Upload branded images and update DB records.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing branded objects.")
    parser.add_argument("--no-db-update", action="store_true")
    parser.add_argument("--no-db-insert", action="store_true")
    parser.add_argument("--no-manifest", action="store_true")
    parser.add_argument("--manifest-limit", type=int, default=5000)
    parser.add_argument("--save-local-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("tophand-branding-report.jsonl"))
    args = parser.parse_args()
    args.update_db = args.write and not args.no_db_update
    args.insert_missing_db_records = args.write and not args.no_db_insert
    return args


def main() -> int:
    args = parse_args()
    if requests is None:
        raise WorkerError("Install the Python 'requests' package before running this worker.")

    load_env_file(args.env)
    args.ollama_url = normalize_ollama_url(args.ollama_url or os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST"))

    supabase_url = require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    supabase_key = require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    client = SupabaseRest(supabase_url, supabase_key)

    if args.write:
        client.ensure_public_bucket(args.dest_bucket)

    camera_filter = set(args.camera) if args.camera else None
    if args.source_path:
        queue = source_paths_to_objects(args.source_path)[: args.limit]
    else:
        queue = list_source_objects(client, args.source_bucket, args.limit, args.min_bytes, camera_filter)
        if args.write and not args.force:
            existing_sources = manifest_source_paths(client, args.dest_bucket)
            before = len(queue)
            queue = [source for source in queue if source.path not in existing_sources]
            skipped = before - len(queue)
            if skipped:
                print(f"Skipped {skipped} source images already present in manifest")
    print(f"Queued {len(queue)} source images from {args.source_bucket}")
    if not queue:
        return 0

    summary = {"uploaded": 0, "dry_run": 0, "skipped_existing": 0, "failed": 0}
    for index, source in enumerate(queue, start=1):
        try:
            result = process_one(client, source, args)
            summary[result["status"]] = summary.get(result["status"], 0) + 1
            print(
                f"[{index}/{len(queue)}] {result['status']}: {source.path} -> "
                f"{result.get('destination_path')} ({result.get('capture_at')}, {result.get('temperature_text')})"
            )
        except Exception as exc:  # noqa: BLE001 - this is a batch worker report boundary
            summary["failed"] += 1
            result = {
                "status": "failed",
                "source_path": source.path,
                "error": str(exc),
            }
            print(f"[{index}/{len(queue)}] failed: {source.path}: {exc}", file=sys.stderr)
        write_report(args.report, result)

    if args.write and not args.no_manifest:
        manifest_count = publish_manifest(client, args.dest_bucket, args.manifest_limit)
        print(f"Manifest updated: {manifest_count} branded images")

    print("Summary:", json.dumps(summary, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
