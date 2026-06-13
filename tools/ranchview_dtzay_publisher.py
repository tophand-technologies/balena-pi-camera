#!/usr/bin/env python3
"""Publish full-size RanchEye images into the dtzay Supabase bucket.

The RanchView PWA at balena-pi-camera.vercel.app reads public images from
dtzayqhebbrbvordmabh.supabase.co/storage/v1/object/public/spypoint-images.
RanchEye currently syncs fresh images into its own Supabase project. This
bridge copies the full-size RanchEye objects into dtzay using the historical
SpyPoint filename pattern expected by the PWA.

Safety rules:
- Source bytes come from RanchEye Supabase storage, not directly from SpyPoint.
- Destination filenames prefer the historical SpyPoint "_S_" name when present,
  but thumbnail_url is used only for the filename, never for bytes.
- Files below --min-bytes are rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import requests
from supabase import create_client


DEFAULT_SOURCE_ENV = Path("/home/travis/rancheye-unified/.env")
DEFAULT_DEST_ENV = Path("/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env")
DEFAULT_SOURCE_API = "http://localhost:8000/api/images"
DEFAULT_REPORT = Path("/home/travis/tophand-instances/sdco/research/ranchview-dtzay-publisher.jsonl")
DEFAULT_BUCKET = "spypoint-images"
LOCAL_TZ = ZoneInfo("America/Chicago")
SAFE_CAMERA_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    key: str
    bucket: str

    @property
    def project_ref(self) -> str:
        match = re.search(r"https://([a-z0-9]+)\.supabase\.co", self.url)
        return match.group(1) if match else self.url


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_supabase_config(path: Path, bucket_default: str) -> SupabaseConfig:
    values = load_env_file(path)
    url = values.get("SUPABASE_URL") or values.get("NEXT_PUBLIC_SUPABASE_URL")
    key = (
        values.get("SUPABASE_SECRET_KEY")
        or values.get("SUPABASE_SERVICE_ROLE_KEY")
        or values.get("SUPABASE_KEY")
        or values.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or values.get("SUPABASE_ANON_KEY")
    )
    bucket = values.get("SUPABASE_BUCKET") or bucket_default
    if not url or not key:
        raise ValueError(f"{path} must define SUPABASE_URL and a usable Supabase key")
    return SupabaseConfig(url=url, key=key, bucket=bucket)


def parse_since(args: argparse.Namespace) -> datetime:
    if args.since:
        local_start = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        return local_start.astimezone(timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=args.since_days)


def api_days_back(since_utc: datetime) -> int:
    now = datetime.now(timezone.utc)
    days = max(1, (now.date() - since_utc.date()).days + 2)
    return min(days, 365)


def parse_rancheye_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filename_from_url(value: str | None) -> str | None:
    if not value:
        return None
    path = unquote(urlparse(value).path)
    name = os.path.basename(path)
    if not name.lower().endswith((".jpg", ".jpeg")):
        return None
    return name


def destination_filename(image: dict[str, Any]) -> tuple[str | None, str | None]:
    metadata = image.get("metadata") or {}
    thumbnail_name = filename_from_url(metadata.get("thumbnail_url"))
    if thumbnail_name:
        return thumbnail_name, "thumbnail_filename"
    hd_name = filename_from_url(metadata.get("hd_url"))
    if hd_name:
        return hd_name, "hd_filename"
    return None, None


def is_safe_spypoint_filename(name: str) -> bool:
    if "thumb" in name.lower():
        return False
    return bool(re.match(r"^PICT[0-9A-Za-z_-]+\.(jpe?g)$", name, re.IGNORECASE))


def destination_path(image: dict[str, Any], filename: str) -> str:
    camera = image.get("camera_name") or image.get("device") or "unknown"
    if not SAFE_CAMERA_RE.match(camera):
        raise ValueError(f"Unsafe camera name: {camera!r}")
    return f"{camera}/{filename}"


def fetch_rancheye_images(source_api: str, since_utc: datetime, limit: int) -> list[dict[str, Any]]:
    days_back = api_days_back(since_utc)
    images: list[dict[str, Any]] = []
    offset = 0

    while len(images) < limit:
        page_limit = min(1000, limit - len(images))
        response = requests.get(
            source_api,
            params={"limit": page_limit, "offset": offset, "days_back": days_back},
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json().get("images") or []
        if not batch:
            break

        for image in batch:
            captured_at = parse_rancheye_time(image.get("captured_at"))
            if captured_at and captured_at >= since_utc:
                images.append(image)

        if len(batch) < page_limit:
            break
        offset += page_limit

    images.sort(key=lambda item: item.get("captured_at") or "")
    return images[:limit]


def list_existing_destinations(dest_client: Any, bucket: str, cameras: set[str]) -> set[str]:
    existing: set[str] = set()
    for camera in sorted(cameras):
        if not SAFE_CAMERA_RE.match(camera):
            continue
        response = dest_client.storage.from_(bucket).list(
            camera,
            {"limit": 1000, "sortBy": {"column": "created_at", "order": "desc"}},
        )
        for item in response or []:
            name = item.get("name")
            item_id = item.get("id")
            if name and item_id:
                existing.add(f"{camera}/{name}")
    return existing


def write_report(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def publish(args: argparse.Namespace) -> int:
    since_utc = parse_since(args)
    source_cfg = read_supabase_config(args.source_env, DEFAULT_BUCKET)
    dest_cfg = read_supabase_config(args.dest_env, DEFAULT_BUCKET)

    if source_cfg.url == dest_cfg.url:
        raise RuntimeError("Source and destination Supabase projects are the same; refusing to run")

    source_client = create_client(source_cfg.url, source_cfg.key)
    dest_client = create_client(dest_cfg.url, dest_cfg.key)
    images = fetch_rancheye_images(args.source_api, since_utc, args.limit)

    candidates: list[tuple[dict[str, Any], str, str, str | None]] = []
    skipped = 0
    for image in images:
        filename, filename_source = destination_filename(image)
        if not filename or not is_safe_spypoint_filename(filename):
            skipped += 1
            continue
        try:
            dest_path = destination_path(image, filename)
        except ValueError:
            skipped += 1
            continue
        candidates.append((image, filename, dest_path, filename_source))

    cameras = {path.split("/", 1)[0] for _, _, path, _ in candidates}
    existing = list_existing_destinations(dest_client, dest_cfg.bucket, cameras)

    summary = {
        "source_project": source_cfg.project_ref,
        "dest_project": dest_cfg.project_ref,
        "since_utc": since_utc.isoformat(),
        "api_images": len(images),
        "candidates": len(candidates),
        "uploaded": 0,
        "already_exists": 0,
        "dry_run": 0,
        "failed": 0,
        "skipped": skipped,
    }

    print(
        f"RanchView dtzay publisher: source={source_cfg.project_ref} "
        f"dest={dest_cfg.project_ref} since={since_utc.isoformat()} candidates={len(candidates)}"
    )

    for image, filename, dest_path, filename_source in candidates:
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "camera": image.get("camera_name"),
            "image_id": image.get("image_id"),
            "captured_at": image.get("captured_at"),
            "source_path": image.get("storage_path"),
            "dest_path": dest_path,
            "filename": filename,
            "filename_source": filename_source,
        }

        if dest_path in existing:
            summary["already_exists"] += 1
            event["status"] = "already_exists"
            write_report(args.report, event)
            continue

        if not args.write:
            summary["dry_run"] += 1
            event["status"] = "dry_run"
            write_report(args.report, event)
            continue

        try:
            source_path = image.get("storage_path")
            if not source_path:
                raise ValueError("missing RanchEye storage_path")

            image_bytes = source_client.storage.from_(source_cfg.bucket).download(source_path)
            byte_count = len(image_bytes)
            if byte_count < args.min_bytes:
                raise ValueError(f"refusing tiny image: {byte_count} bytes")

            dest_client.storage.from_(dest_cfg.bucket).upload(
                dest_path,
                image_bytes,
                {"content-type": "image/jpeg", "upsert": "false"},
            )
            summary["uploaded"] += 1
            existing.add(dest_path)
            event["status"] = "uploaded"
            event["bytes"] = byte_count
            print(f"uploaded {dest_path} ({byte_count} bytes)")
        except Exception as exc:  # noqa: BLE001 - report and continue per image
            summary["failed"] += 1
            event["status"] = "failed"
            event["error"] = str(exc)[:500]
            print(f"failed {dest_path}: {exc}", file=sys.stderr)

        write_report(args.report, event)

    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, default=DEFAULT_SOURCE_ENV)
    parser.add_argument("--dest-env", type=Path, default=DEFAULT_DEST_ENV)
    parser.add_argument("--source-api", default=DEFAULT_SOURCE_API)
    parser.add_argument("--since", help="Local date YYYY-MM-DD. Overrides --since-days.")
    parser.add_argument("--since-days", type=int, default=3)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--min-bytes", type=int, default=5000)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--write", action="store_true", help="Actually upload. Omit for dry-run.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return publish(args)


if __name__ == "__main__":
    raise SystemExit(main())
