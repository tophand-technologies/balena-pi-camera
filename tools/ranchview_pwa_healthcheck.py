#!/usr/bin/env python3
"""Health check for the RanchView Image Gallery PWA feed.

This checks the live chain that keeps balena-pi-camera.vercel.app fresh:
RanchEye API -> expected dtzay object paths -> public Supabase image URLs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ranchview_dtzay_publisher import (
    DEFAULT_BUCKET,
    DEFAULT_DEST_ENV,
    DEFAULT_SOURCE_API,
    DEFAULT_SOURCE_ENV,
    destination_filename,
    destination_path,
    is_safe_spypoint_filename,
    parse_rancheye_time,
    read_supabase_config,
)


DEFAULT_STATUS_FILE = Path("/home/travis/tophand-instances/sdco/research/ranchview-pwa-healthcheck.json")
DEFAULT_PUBLISHER_TIMER = "ranchview-dtzay-publisher.timer"
DEFAULT_PUBLISHER_SERVICE = "ranchview-dtzay-publisher.service"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, **details: Any) -> None:
    checks.append({"name": name, "ok": ok, **details})


def run_command(command: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - health checks should report, not crash
        return 999, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def systemctl_show(unit: str) -> dict[str, str]:
    code, stdout, stderr = run_command(["systemctl", "show", unit])
    if code != 0:
        return {"error": stderr or stdout or f"systemctl show exited {code}"}

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def check_systemd(args: argparse.Namespace, checks: list[dict[str, Any]]) -> None:
    if args.skip_systemd:
        add_check(checks, "systemd", True, skipped=True)
        return

    code, enabled, enabled_err = run_command(["systemctl", "is-enabled", args.publisher_timer])
    add_check(
        checks,
        "publisher_timer_enabled",
        code == 0 and enabled == "enabled",
        unit=args.publisher_timer,
        value=enabled,
        error=enabled_err,
    )

    code, active, active_err = run_command(["systemctl", "is-active", args.publisher_timer])
    add_check(
        checks,
        "publisher_timer_active",
        code == 0 and active == "active",
        unit=args.publisher_timer,
        value=active,
        error=active_err,
    )

    service = systemctl_show(args.publisher_service)
    result = service.get("Result")
    status = service.get("ExecMainStatus")
    add_check(
        checks,
        "publisher_last_result",
        result in ("success", None, "") and status in ("0", None, ""),
        unit=args.publisher_service,
        result=result,
        exec_main_status=status,
        active_state=service.get("ActiveState"),
        error=service.get("error"),
    )

    timer = systemctl_show(args.publisher_timer)
    add_check(
        checks,
        "publisher_timer_schedule",
        bool(timer.get("NextElapseUSecMonotonic")),
        unit=args.publisher_timer,
        last_trigger=timer.get("LastTriggerUSec"),
        next_trigger=timer.get("NextElapseUSecMonotonic"),
        error=timer.get("error"),
    )


def fetch_source_images(source_api: str, limit: int, days_back: int) -> list[dict[str, Any]]:
    response = requests.get(
        source_api,
        params={"limit": limit, "days_back": days_back},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("images") or []


def build_candidates(images: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    skipped = 0

    for image in images:
        captured_at = parse_rancheye_time(image.get("captured_at"))
        filename, filename_source = destination_filename(image)
        if not captured_at or not filename or not is_safe_spypoint_filename(filename):
            skipped += 1
            continue
        try:
            dest_path = destination_path(image, filename)
        except ValueError:
            skipped += 1
            continue

        candidates.append(
            {
                "camera": image.get("camera_name"),
                "image_id": image.get("image_id"),
                "captured_at": captured_at.isoformat(),
                "dest_path": dest_path,
                "filename_source": filename_source,
                "source_path": image.get("storage_path"),
                "_captured_at_dt": captured_at,
            }
        )

    candidates.sort(key=lambda item: item["_captured_at_dt"], reverse=True)
    return candidates[:limit], skipped


def public_object_url(supabase_url: str, bucket: str, object_path: str) -> str:
    quoted_path = quote(object_path, safe="/")
    return f"{supabase_url}/storage/v1/object/public/{bucket}/{quoted_path}"


def probe_public_object(url: str, timeout: int) -> dict[str, Any]:
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code == 405:
            response = requests.get(url, stream=True, allow_redirects=True, timeout=timeout)
        return {
            "status_code": response.status_code,
            "content_length": response.headers.get("content-length"),
            "content_type": response.headers.get("content-type"),
        }
    except Exception as exc:  # noqa: BLE001 - report failing object
        return {"status_code": None, "error": str(exc)}


def check_public_destinations(
    args: argparse.Namespace,
    checks: list[dict[str, Any]],
    dest_url: str,
    bucket: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []

    for candidate in candidates[: args.public_head_limit]:
        url = public_object_url(dest_url, bucket, candidate["dest_path"])
        probe = probe_public_object(url, args.public_timeout)
        content_length_raw = probe.get("content_length")
        try:
            content_length = int(content_length_raw) if content_length_raw else None
        except ValueError:
            content_length = None

        ok = probe.get("status_code") == 200 and (
            content_length is None or content_length >= args.min_public_bytes
        )

        probes.append(
            {
                "dest_path": candidate["dest_path"],
                "captured_at": candidate["captured_at"],
                "camera": candidate["camera"],
                "status_code": probe.get("status_code"),
                "content_length": content_length,
                "content_type": probe.get("content_type"),
                "ok": ok,
                "error": probe.get("error"),
            }
        )

    missing = [probe for probe in probes if not probe["ok"]]
    add_check(
        checks,
        "pwa_public_objects",
        not missing and bool(probes),
        checked=len(probes),
        failed=len(missing),
        failures=missing[:5],
    )
    return probes


def remove_private_fields(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        public_candidates.append({key: value for key, value in candidate.items() if not key.startswith("_")})
    return public_candidates


def write_status(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, default=DEFAULT_SOURCE_ENV)
    parser.add_argument("--dest-env", type=Path, default=DEFAULT_DEST_ENV)
    parser.add_argument("--source-api", default=DEFAULT_SOURCE_API)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--days-back", type=int, default=10)
    parser.add_argument("--max-source-age-hours", type=float, default=48)
    parser.add_argument("--public-head-limit", type=int, default=10)
    parser.add_argument("--public-timeout", type=int, default=15)
    parser.add_argument("--min-public-bytes", type=int, default=5000)
    parser.add_argument("--publisher-timer", default=DEFAULT_PUBLISHER_TIMER)
    parser.add_argument("--publisher-service", default=DEFAULT_PUBLISHER_SERVICE)
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checks: list[dict[str, Any]] = []
    now = utc_now()

    try:
        source_cfg = read_supabase_config(args.source_env, args.bucket)
        dest_cfg = read_supabase_config(args.dest_env, args.bucket)
        add_check(
            checks,
            "config",
            source_cfg.url != dest_cfg.url,
            source_project=source_cfg.project_ref,
            dest_project=dest_cfg.project_ref,
            bucket=dest_cfg.bucket,
        )
    except Exception as exc:  # noqa: BLE001
        add_check(checks, "config", False, error=str(exc))
        summary = {
            "ok": False,
            "checked_at": now.isoformat(),
            "checks": checks,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 2

    check_systemd(args, checks)

    try:
        images = fetch_source_images(args.source_api, args.limit, args.days_back)
        add_check(checks, "rancheye_api", bool(images), count=len(images), source_api=args.source_api)
    except Exception as exc:  # noqa: BLE001
        add_check(checks, "rancheye_api", False, source_api=args.source_api, error=str(exc))
        images = []

    candidates, skipped = build_candidates(images, args.limit)
    add_check(checks, "source_candidates", bool(candidates), count=len(candidates), skipped=skipped)

    latest_age_hours: float | None = None
    if candidates:
        latest_dt = candidates[0]["_captured_at_dt"]
        raw_latest_age_hours = (now - latest_dt).total_seconds() / 3600
        latest_age_hours = max(0.0, raw_latest_age_hours)
        add_check(
            checks,
            "source_freshness",
            latest_age_hours <= args.max_source_age_hours,
            latest_captured_at=candidates[0]["captured_at"],
            latest_age_hours=round(latest_age_hours, 2),
            raw_latest_age_hours=round(raw_latest_age_hours, 2),
            max_source_age_hours=args.max_source_age_hours,
        )
    else:
        add_check(checks, "source_freshness", False, error="no usable source candidates")

    probes = check_public_destinations(args, checks, dest_cfg.url, dest_cfg.bucket, candidates)

    summary = {
        "ok": all(check["ok"] for check in checks),
        "checked_at": now.isoformat(),
        "source_project": source_cfg.project_ref,
        "dest_project": dest_cfg.project_ref,
        "bucket": dest_cfg.bucket,
        "latest_source_age_hours": round(latest_age_hours, 2) if latest_age_hours is not None else None,
        "latest_candidates": remove_private_fields(candidates[:5]),
        "public_probes": probes,
        "checks": checks,
    }

    try:
        write_status(args.status_file, summary)
    except Exception as exc:  # noqa: BLE001
        add_check(checks, "status_file_write", False, path=str(args.status_file), error=str(exc))
        summary["ok"] = False
        summary["checks"] = checks

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        state = "OK" if summary["ok"] else "FAILED"
        print(f"RanchView PWA health: {state}")
        for check in checks:
            prefix = "OK" if check["ok"] else "FAIL"
            detail = {key: value for key, value in check.items() if key not in {"name", "ok"}}
            print(f"{prefix} {check['name']} {json.dumps(detail, sort_keys=True)}")
        print(f"status_file={args.status_file}")

    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
