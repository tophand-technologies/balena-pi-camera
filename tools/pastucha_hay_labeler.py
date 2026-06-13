#!/usr/bin/env python3
"""Small browser UI for creating Pastucha Hay golden labels.

This is intentionally dependency-light: stdlib HTTP server plus the existing
Supabase helper from `tophand_branding_worker.py`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import tophand_branding_worker as branding
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Run this from a checkout containing tools/tophand_branding_worker.py") from exc


CAMERA_ID = "FLEX-M-MGE4"
CAMERA_TITLE = "Pastucha Hay"
DEST_BUCKET = branding.DEST_BUCKET
DEFAULT_DATA_DIR = Path("/home/travis/tophand-instances/sdco/research/pastucha-hay")
DEFAULT_DATA_ROOT = Path("/home/travis/tophand-instances/sdco/research")


@dataclass(frozen=True)
class CameraConfig:
    slug: str
    route_path: str
    camera_id: str
    camera_title: str
    page_title: str
    subtitle: str
    schema_version: str
    data_dir: Path
    source_queue_path: Path
    bale_slot_count: int
    water_section_title: str | None = None


NAV_ORDER = ("pastucha-hay", "donna-trough-2", "donna-trough-1", "pastucha-pond")


def camera_configs(data_root: Path, pastucha_data_dir: Path, pastucha_source_queue: Path | None) -> dict[str, CameraConfig]:
    pastucha_source = pastucha_source_queue or (pastucha_data_dir / "source_queue.json")
    donna1_dir = data_root / "donna-trough-1"
    donna_dir = data_root / "donna-trough-2"
    pond_dir = data_root / "pastucha-pond"
    return {
        "pastucha-hay": CameraConfig(
            slug="pastucha-hay",
            route_path="",
            camera_id="FLEX-M-MGE4",
            camera_title="Pastucha Hay",
            page_title="Pastucha Hay Golden Labels",
            subtitle="FLEX-M-MGE4 ROUND BALE RESEARCH",
            schema_version="pastucha_hay_label_v3",
            data_dir=pastucha_data_dir,
            source_queue_path=pastucha_source,
            bale_slot_count=4,
        ),
        "donna-trough-2": CameraConfig(
            slug="donna-trough-2",
            route_path="/donna-trough-2",
            camera_id="YV",
            camera_title="Donna Trough 2",
            page_title="Donna Trough 2 Golden Labels",
            subtitle="YV WATER TROUGH + SINGLE BALE RESEARCH",
            schema_version="donna_trough_2_label_v1",
            data_dir=donna_dir,
            source_queue_path=donna_dir / "source_queue.json",
            bale_slot_count=1,
            water_section_title="Donna Trough 2",
        ),
        "donna-trough-1": CameraConfig(
            slug="donna-trough-1",
            route_path="/donna-trough-1",
            camera_id="QN",
            camera_title="Donna Trough 1",
            page_title="Donna Trough 1 Golden Labels",
            subtitle="QN WATER TROUGH + RANCH SCENE RESEARCH",
            schema_version="donna_trough_1_label_v1",
            data_dir=donna1_dir,
            source_queue_path=donna1_dir / "source_queue.json",
            bale_slot_count=1,
            water_section_title="Donna Trough 1",
        ),
        "pastucha-pond": CameraConfig(
            slug="pastucha-pond",
            route_path="/pastucha-pond",
            camera_id="QC",
            camera_title="Pastucha Pond",
            page_title="Pastucha Pond Golden Labels",
            subtitle="QC POND WATER + RANCH SCENE RESEARCH",
            schema_version="pastucha_pond_label_v1",
            data_dir=pond_dir,
            source_queue_path=pond_dir / "source_queue.json",
            bale_slot_count=1,
            water_section_title="Pastucha Pond",
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pastucha Hay golden-label UI")
    parser.add_argument("--env", type=Path, default=Path("/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--manifest-path", default="manifest.json")
    parser.add_argument("--source-bucket", default=branding.SOURCE_BUCKET)
    parser.add_argument("--source-queue", type=Path)
    return parser.parse_args()


def parse_time(value: str | None) -> dt.datetime:
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


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def compact_number(value: float | None, digits: int = 1) -> float | int | None:
    if value is None:
        return None
    rounded = round(value, digits)
    return int(rounded) if float(rounded).is_integer() else rounded


def bale_equivalents(row: dict[str, Any]) -> float | None:
    explicit = numeric(row.get("bale_equivalents_remaining"))
    if explicit is not None:
        return explicit
    total = 0.0
    found = False
    for slot in range(1, 5):
        percent = numeric(row.get(f"bale_{slot}_remaining_percent"))
        if percent is not None:
            found = True
            total += max(0.0, percent) / 100.0
    return total if found else None


def round_bales_visible(row: dict[str, Any]) -> int | None:
    explicit = numeric(row.get("round_bales_visible"))
    if explicit is not None:
        return int(round(explicit))
    count = 0
    found = False
    for slot in range(1, 5):
        percent = numeric(row.get(f"bale_{slot}_remaining_percent"))
        present = bool(row.get(f"bale_{slot}_present"))
        if present or (percent is not None and percent > 0):
            count += 1
            found = True
    return count if found else None


def no_bales_confirmed(row: dict[str, Any]) -> bool:
    if row.get("no_bales_confirmed"):
        return True
    visible = round_bales_visible(row)
    equivalent = bale_equivalents(row)
    return visible == 0 and equivalent == 0


def cattle_count(row: dict[str, Any]) -> int | None:
    explicit = numeric(row.get("cattle_count"))
    if explicit is not None:
        return int(round(explicit))
    parts = [numeric(row.get(key)) for key in ("cow_count", "calf_count", "bull_count")]
    if all(value is None for value in parts):
        return None
    return int(round(sum(value or 0 for value in parts)))


def label_bale_slots(row: dict[str, Any]) -> list[dict[str, Any]]:
    slots = row.get("bale_slots") or row.get("bales") or []
    if isinstance(slots, list) and slots:
        return [slot for slot in slots if isinstance(slot, dict)]
    output = []
    for slot in range(1, 5):
        percent = numeric(row.get(f"bale_{slot}_remaining_percent"))
        present = bool(row.get(f"bale_{slot}_present")) or (percent is not None and percent > 0)
        if not present and percent is None:
            continue
        output.append(
            {
                "slot": slot,
                "present": present,
                "location": row.get(f"bale_{slot}_location"),
                "remaining_percent": compact_number(percent, 0),
                "condition": row.get(f"bale_{slot}_condition"),
                "color_quality": row.get(f"bale_{slot}_color_quality"),
                "hay_ring_visible": bool(row.get(f"bale_{slot}_hay_ring_visible")),
                "scatter_present": bool(row.get(f"bale_{slot}_scatter_present")),
                "scatter_level": row.get(f"bale_{slot}_scatter_level"),
                "visibility": row.get(f"bale_{slot}_visibility"),
                "level_confidence": row.get(f"bale_{slot}_level_confidence"),
                "occlusion_level": row.get(f"bale_{slot}_occlusion_level"),
                "occluded_by": row.get(f"bale_{slot}_occluded_by"),
            }
        )
    return output


class LabelStore:
    def __init__(self, data_dir: Path, schema_version: str = "pastucha_hay_label_v3") -> None:
        self.data_dir = data_dir
        self.schema_version = schema_version
        self.latest_path = data_dir / "golden_labels.latest.json"
        self.jsonl_path = data_dir / "golden_labels.jsonl"
        self.latest: dict[str, Any] = self.canonicalize(read_json(self.latest_path, {}))

    @staticmethod
    def label_key(payload: dict[str, Any]) -> str:
        return str(payload.get("source_path") or payload.get("path") or "")

    @staticmethod
    def is_newer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        candidate_time = parse_time(candidate.get("updated_at") or candidate.get("captured_at"))
        current_time = parse_time(current.get("updated_at") or current.get("captured_at"))
        return candidate_time >= current_time

    def canonicalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        canonical: dict[str, Any] = {}
        changed = False
        for fallback_key, value in payload.items():
            if not isinstance(value, dict):
                continue
            row = dict(value)
            key = self.label_key(row) or str(fallback_key)
            if key != fallback_key:
                changed = True
            existing = canonical.get(key)
            if existing is None or self.is_newer(row, existing):
                canonical[key] = row
        if changed:
            write_json(self.latest_path, canonical)
        return canonical

    def get(self, *image_paths: str | None) -> dict[str, Any] | None:
        for image_path in image_paths:
            if not image_path:
                continue
            value = self.latest.get(image_path)
            if isinstance(value, dict):
                return value
        return None

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        image_path = self.label_key(payload)
        if not image_path:
            raise ValueError("Missing image path")
        now = dt.datetime.now(dt.UTC).isoformat()
        payload["updated_at"] = now
        payload.setdefault("schema_version", self.schema_version)
        self.latest[image_path] = payload
        write_json(self.latest_path, self.latest)
        append_jsonl(self.jsonl_path, payload)
        return payload

    def sorted_labels(self) -> list[dict[str, Any]]:
        rows = [row for row in self.latest.values() if isinstance(row, dict) and row.get("captured_at")]
        rows.sort(key=lambda row: parse_time(row.get("captured_at")))
        return rows

    def intelligence_from_label(
        self,
        label: dict[str, Any],
        status: str = "human",
        confidence_score: float = 1.0,
        basis: str = "human label",
    ) -> dict[str, Any]:
        no_bales = no_bales_confirmed(label)
        visible = round_bales_visible(label)
        equivalent = bale_equivalents(label)
        cattle = cattle_count(label)
        if no_bales:
            summary = "No bales confirmed"
        elif equivalent is not None:
            bale_word = "bale" if visible == 1 else "bales"
            summary = f"{visible or 0} {bale_word}, about {compact_number(equivalent)} bale equivalents"
        elif visible is not None:
            bale_word = "bale" if visible == 1 else "bales"
            summary = f"{visible} {bale_word} visible"
        else:
            summary = "Hay state needs review"
        if cattle:
            summary = f"{summary}; {cattle} cattle visible"
        return {
            "status": status,
            "analysis_source": "human_label" if status == "human" else "timeline_draft",
            "basis": basis,
            "summary": summary,
            "no_bales_confirmed": no_bales,
            "round_bales_visible": visible,
            "bale_equivalents_remaining": compact_number(equivalent),
            "hay_days_remaining": compact_number(numeric(label.get("hay_days_remaining"))),
            "new_bales_put_out": bool(label.get("new_bales_put_out")),
            "cattle_present": bool(label.get("cattle_present")) or bool(cattle),
            "cattle_count": cattle,
            "cow_count": compact_number(numeric(label.get("cow_count")), 0),
            "calf_count": compact_number(numeric(label.get("calf_count")), 0),
            "bull_count": compact_number(numeric(label.get("bull_count")), 0),
            "bale_slots": label_bale_slots(label),
            "confidence_score": round(confidence_score, 2),
        }

    def draft_intelligence(self, image: dict[str, Any]) -> dict[str, Any]:
        captured = parse_time(image.get("captured_at"))
        labels = self.sorted_labels()
        if not labels or captured == dt.datetime.min.replace(tzinfo=dt.UTC):
            return {
                "status": "needs_review",
                "analysis_source": "timeline_draft",
                "basis": "no nearby labels",
                "summary": "Hay intelligence needs a human label",
                "confidence_score": 0.1,
            }

        source_path = image.get("source_path") or image.get("path")
        before = None
        after = None
        for label in labels:
            if self.label_key(label) == source_path:
                continue
            label_time = parse_time(label.get("captured_at"))
            if label_time <= captured:
                before = label
            elif after is None:
                after = label
                break

        candidates = []
        for direction, label in (("before", before), ("after", after)):
            if label:
                hours = abs((captured - parse_time(label.get("captured_at"))).total_seconds()) / 3600
                candidates.append((hours, direction, label))
        if not candidates:
            return {
                "status": "needs_review",
                "analysis_source": "timeline_draft",
                "basis": "no nearby labels",
                "summary": "Hay intelligence needs a human label",
                "confidence_score": 0.1,
            }

        nearest_hours, nearest_direction, nearest_label = sorted(candidates, key=lambda item: item[0])[0]
        confidence = max(0.25, min(0.72, 0.78 - nearest_hours * 0.018))

        if before and after:
            before_time = parse_time(before.get("captured_at"))
            after_time = parse_time(after.get("captured_at"))
            span_hours = max((after_time - before_time).total_seconds() / 3600, 0.01)
            before_hours = abs((captured - before_time).total_seconds()) / 3600
            after_hours = abs((after_time - captured).total_seconds()) / 3600
            before_eq = bale_equivalents(before)
            after_eq = bale_equivalents(after)
            before_no = no_bales_confirmed(before)
            after_no = no_bales_confirmed(after)
            if before_hours <= 36 and after_hours <= 36 and before_no and after_no:
                draft = self.intelligence_from_label(before, "draft", 0.78, "nearby labels before and after both say no bales")
                draft["summary"] = "Likely no bales; nearby labels agree"
                draft["nearest_label_hours"] = compact_number(min(before_hours, after_hours))
                return draft
            if before_eq is not None and after_eq is not None and before_hours <= 48 and after_hours <= 48:
                ratio = min(1.0, max(0.0, before_hours / span_hours))
                estimate = before_eq + (after_eq - before_eq) * ratio
                visible = round_bales_visible(before if before_hours <= after_hours else after)
                no_bales = estimate <= 0.08 and before_no and after_no
                draft = self.intelligence_from_label(nearest_label, "draft", min(0.76, confidence + 0.08), "interpolated between nearby labels")
                draft["bale_equivalents_remaining"] = compact_number(max(0.0, estimate))
                draft["round_bales_visible"] = 0 if no_bales else visible
                draft["no_bales_confirmed"] = no_bales
                draft["nearest_label_hours"] = compact_number(min(before_hours, after_hours))
                if no_bales:
                    draft["summary"] = "Draft: likely no bales from nearby labels"
                else:
                    draft["summary"] = f"Draft: about {compact_number(max(0.0, estimate))} bale equivalents from nearby labels"
                return draft

        draft = self.intelligence_from_label(
            nearest_label,
            "draft",
            confidence,
            f"copied from nearest {nearest_direction} label",
        )
        draft["nearest_label_hours"] = compact_number(nearest_hours)
        if nearest_hours > 48:
            draft["status"] = "needs_review"
            draft["confidence_score"] = 0.2
            draft["summary"] = "Needs review; nearest hay label is too far away"
        else:
            draft["summary"] = f"Draft: {draft['summary']}"
        return draft

    def hay_intelligence(self, image: dict[str, Any], label: dict[str, Any] | None) -> dict[str, Any]:
        if label:
            return self.intelligence_from_label(label)
        return self.draft_intelligence(image)


class ImageIndex:
    def __init__(
        self,
        client: branding.SupabaseRest,
        config: CameraConfig,
        manifest_path: str,
        source_bucket: str,
    ) -> None:
        self.client = client
        self.config = config
        self.manifest_path = manifest_path
        self.source_bucket = source_bucket
        self.images: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        manifest = self.client.download_json_optional(DEST_BUCKET, self.manifest_path)
        if not manifest:
            raise RuntimeError(f"Could not load {DEST_BUCKET}/{self.manifest_path}")
        images = []
        for item in manifest.get("images", []):
            if item.get("device") != self.config.camera_id:
                continue
            row = dict(item)
            row["public_url"] = self.client.public_url(DEST_BUCKET, row["path"])
            row["sort_time"] = parse_time(row.get("captured_at")).isoformat()
            row["image_mode"] = "branded"
            images.append(row)

        seen_sources = {row.get("source_path") or row.get("path") for row in images}
        if self.config.source_queue_path and self.config.source_queue_path.exists():
            queue = read_json(self.config.source_queue_path, {})
            for item in queue.get("images", []):
                if item.get("device") != self.config.camera_id:
                    continue
                if not item.get("overlay_verified"):
                    continue
                if not item.get("captured_at"):
                    continue
                if not str(item.get("capture_time_source") or "").startswith("image_overlay_"):
                    continue
                source_path = item.get("source_path") or item.get("path")
                if not source_path or source_path in seen_sources:
                    continue
                row = dict(item)
                row["path"] = source_path
                row["source_path"] = source_path
                row["public_url"] = self.client.public_url(self.source_bucket, source_path)
                row["sort_time"] = parse_time(row.get("captured_at")).isoformat()
                row["camera_title"] = self.config.camera_title
                row["image_mode"] = "source"
                images.append(row)
                seen_sources.add(source_path)
        images.sort(key=lambda row: parse_time(row.get("captured_at")), reverse=True)
        self.images = images

    def query(self, params: dict[str, list[str]], labels: LabelStore) -> list[dict[str, Any]]:
        start = (params.get("start") or [""])[0]
        end = (params.get("end") or [""])[0]
        limit = int((params.get("limit") or ["300"])[0] or 300)
        unlabeled_only = (params.get("unlabeled") or ["0"])[0] in {"1", "true", "yes"}

        start_dt = dt.datetime.fromisoformat(start).replace(tzinfo=branding.CAPTURE_TZ) if start else None
        end_dt = dt.datetime.fromisoformat(end).replace(hour=23, minute=59, second=59, tzinfo=branding.CAPTURE_TZ) if end else None

        rows = []
        for image in self.images:
            captured = parse_time(image.get("captured_at"))
            if start_dt and captured < start_dt:
                continue
            if end_dt and captured > end_dt:
                continue
            existing = labels.get(image.get("source_path"), image.get("path"))
            if unlabeled_only and existing:
                continue
            row = dict(image)
            row["label"] = existing
            row["hay_intelligence"] = labels.hay_intelligence(row, existing)
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows


def nav_html(active_slug: str, configs: dict[str, CameraConfig]) -> str:
    links = []
    for slug in NAV_ORDER:
        if slug not in configs:
            continue
        config = configs[slug]
        href = config.route_path or "/"
        active = " active" if slug == active_slug else ""
        links.append(f'<a class="camera-link{active}" href="{html.escape(href)}">{html.escape(config.camera_title)}</a>')
    return '<nav class="camera-nav">' + "".join(links) + "</nav>"


def range_options(config: CameraConfig) -> str:
    if config.slug == "donna-trough-2":
        options = [
            ("", "Custom / recent"),
            ("2026-01-19:2026-04-15", "Stable trough era"),
            ("2026-04-14:2026-04-15", "Apr 14-15 branded"),
            ("2026-03-01:2026-04-15", "Mar-Apr"),
            ("2026-01-19:2026-01-31", "Jan 19-31"),
        ]
    elif config.slug == "donna-trough-1":
        options = [
            ("", "Custom / recent"),
            ("2026-04-01:2026-04-28", "April"),
            ("2026-03-01:2026-04-28", "Mar-Apr"),
            ("2026-01-01:2026-04-28", "2026 history"),
        ]
    elif config.slug == "pastucha-pond":
        options = [
            ("", "Custom / recent"),
            ("2026-04-01:2026-04-28", "April"),
            ("2026-03-01:2026-04-28", "Mar-Apr"),
            ("2026-01-01:2026-04-28", "2026 history"),
        ]
    else:
        options = [
            ("", "Custom / recent"),
            ("2026-01-17:2026-04-26", "All history"),
            ("2026-01-17:2026-01-22", "Jan 17-22"),
            ("2026-01-23:2026-01-30", "Jan 23-30"),
            ("2026-02-15:2026-02-21", "Feb 15-21"),
            ("2026-03-04:2026-03-12", "Mar 4-12"),
        ]
    return "\n".join(f'<option value="{html.escape(value)}">{html.escape(label)}</option>' for value, label in options)


def html_page(config: CameraConfig, configs: dict[str, CameraConfig]) -> str:
    body_classes = [f"camera-{config.slug}"]
    if config.water_section_title:
        body_classes.append("water-watch")
    if config.bale_slot_count == 1:
        body_classes.append("single-bale")
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__PAGE_TITLE__</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #151515;
      --panel: #242424;
      --panel-2: #303030;
      --text: #f2f2f2;
      --muted: #aaa;
      --line: #444;
      --gold: #d6b56d;
      --green: #49b35a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: #111;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { font-size: 20px; margin: 0; }
    .sub { color: var(--gold); font-size: 12px; font-weight: 700; letter-spacing: .1em; }
    .camera-nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .camera-link {
      color: var(--text);
      text-decoration: none;
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 8px 10px;
      border-radius: 6px;
      font-weight: 800;
      font-size: 13px;
    }
    .camera-link.active { background: #436a45; border-color: #5c9160; }
    .filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
    label { display: grid; gap: 4px; color: var(--muted); font-size: 12px; }
    input, select, textarea, button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
    }
    button { cursor: pointer; font-weight: 700; }
    button.primary { background: var(--green); border-color: var(--green); color: white; }
    main {
      display: grid;
      grid-template-columns: 300px minmax(420px, 1fr) 440px;
      min-height: calc(100vh - 74px);
    }
    aside, section { min-width: 0; }
    .list {
      border-right: 1px solid var(--line);
      overflow: auto;
      max-height: calc(100vh - 74px);
    }
    .item {
      display: grid;
      grid-template-columns: 72px 1fr;
      gap: 10px;
      padding: 10px;
      border-bottom: 1px solid #333;
      cursor: pointer;
    }
    .item.active { background: #333; outline: 1px solid var(--gold); }
    .item img { width: 72px; height: 54px; object-fit: cover; border-radius: 4px; }
    .item strong { display: block; font-size: 13px; }
    .item span { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
    .badge { color: var(--gold); font-weight: 700; }
    .viewer {
      padding: 18px;
      display: grid;
      align-content: start;
      gap: 12px;
    }
    .viewer img {
      width: 100%;
      max-height: 76vh;
      object-fit: contain;
      background: #050505;
      border-radius: 8px;
    }
    .meta { color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; }
    .hay-intel {
      border: 1px solid rgba(214, 181, 109, .28);
      background: rgba(214, 181, 109, .08);
      border-radius: 8px;
      padding: 10px 12px;
      display: grid;
      gap: 8px;
    }
    .hay-intel.draft { border-color: rgba(73, 179, 90, .34); background: rgba(73, 179, 90, .08); }
    .hay-intel.needs_review { border-color: rgba(200, 200, 200, .24); background: rgba(255, 255, 255, .05); }
    .hay-intel-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--gold);
      font-weight: 800;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .hay-intel-summary { font-size: 15px; font-weight: 700; }
    .hay-intel-basis { color: var(--muted); font-size: 12px; }
    .hay-chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .hay-chip {
      border: 1px solid rgba(214, 181, 109, .25);
      border-radius: 999px;
      padding: 4px 8px;
      color: #ead7a5;
      font-size: 12px;
      font-weight: 700;
      background: rgba(0, 0, 0, .16);
    }
    .item-hay {
      color: #d7c28d;
      font-size: 12px;
      margin-top: 3px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .form {
      border-left: 1px solid var(--line);
      padding: 18px;
      overflow: auto;
      max-height: calc(100vh - 74px);
      background: #1d1d1d;
    }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .bale-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
    .bale-slot {
      background: var(--panel);
      border: 1px solid #383838;
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .bale-slot h4 {
      margin: 0;
      color: var(--gold);
      font-size: 13px;
    }
    .bale-slot .slot-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .bale-slot .slot-title label {
      display: flex;
      grid-auto-flow: column;
      align-items: center;
      gap: 6px;
      color: var(--text);
      font-size: 12px;
    }
    .checks { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
    .checks label {
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--panel);
      padding: 8px 9px;
      border-radius: 6px;
    }
    input[readonly] { opacity: .72; }
    input:disabled, select:disabled, textarea:disabled { opacity: .48; }
    .water-only { display: none; }
    .water-watch .water-only { display: block; }
    .single-bale .bale-grid .bale-slot:nth-child(n+2) { display: none; }
    .actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
    .top-actions {
      position: sticky;
      top: 0;
      z-index: 4;
      margin: -18px -18px 16px;
      padding: 12px 18px;
      background: rgba(29, 29, 29, .96);
      border-bottom: 1px solid var(--line);
    }
    button.warning {
      background: #7d5d28;
      border-color: #9a7534;
      color: white;
    }
    .status { color: var(--gold); min-height: 20px; margin-top: 10px; }
    @media (max-width: 1100px) {
      main { grid-template-columns: 1fr; }
      .list, .form { max-height: none; border: 0; }
    }
  </style>
</head>
<body class="__BODY_CLASSES__">
  <header>
    <div>
      <h1>__PAGE_TITLE__</h1>
      <div class="sub">__PAGE_SUBTITLE__</div>
      __CAMERA_NAV__
    </div>
    <div class="filters">
      <label>Start <input id="start" type="date"></label>
      <label>End <input id="end" type="date"></label>
      <label>Limit <input id="limit" type="number" value="300" min="1" max="1000"></label>
      <label>Range
        <select id="range_preset">
          __RANGE_OPTIONS__
        </select>
      </label>
      <label><span>&nbsp;</span><select id="unlabeled"><option value="0">All</option><option value="1">Unlabeled only</option></select></label>
      <button id="load" class="primary">Load</button>
    </div>
  </header>
  <main>
    <aside class="list" id="list"></aside>
    <section class="viewer">
      <img id="image" alt="">
      <div class="meta" id="meta"></div>
      <div id="hay_intel"></div>
      <div class="actions">
        <button id="prev">Previous</button>
        <button id="next">Next</button>
      </div>
    </section>
    <section class="form">
      <div class="actions top-actions">
        <button id="save_top" class="primary">Save Label</button>
        <button id="save_draft_top" class="warning">Save Draft</button>
        <button id="no_bales_save_top">No Bales + Save</button>
      </div>
      <h2 style="margin-top:0">Your Interpretation</h2>
      <div class="checks">
        <label><input id="no_bales_confirmed" type="checkbox"> No bales confirmed</label>
      </div>
      <div class="grid2">
        <label>Round bales visible <input id="round_bales_visible" type="number" min="0" max="10"></label>
        <label>Bale equivalents remaining <input id="bale_equivalents_remaining" type="number" min="0" max="10" step="0.1"></label>
        <label>Estimated hay days remaining <input id="hay_days_remaining" type="number" min="0" max="30" step="0.5"></label>
        <label>Total cattle <input id="cattle_count" type="number" min="0" max="200" readonly></label>
      </div>
      <h3>Animals</h3>
      <div class="grid3">
        <label>Cows <input id="cow_count" type="number" min="0" max="200"></label>
        <label>Calves <input id="calf_count" type="number" min="0" max="200"></label>
        <label>Bulls <input id="bull_count" type="number" min="0" max="20"></label>
      </div>
      <div class="checks">
        <label><input id="cattle_present" type="checkbox"> Cattle present</label>
      </div>
      <div class="water-only">
        <h3>__WATER_SECTION_TITLE__</h3>
        <div class="grid2">
          <label>Horses <input id="horse_count" type="number" min="0" max="20"></label>
          <label>Water level % <input id="water_level_percent" type="number" min="0" max="100"></label>
        </div>
        <div class="checks">
          <label><input id="horse_present" type="checkbox"> Horse present</label>
          <label><input id="longhorn_cow_present" type="checkbox"> Longhorn cow present</label>
          <label><input id="water_trough_visible" type="checkbox"> Trough visible</label>
          <label><input id="water_visible" type="checkbox"> Water visible</label>
          <label><input id="float_pipe_visible" type="checkbox"> Float / pipe visible</label>
          <label><input id="feed_tub_visible" type="checkbox"> Feed tub visible</label>
        </div>
        <div class="grid2" style="margin-top:10px">
          <label>Water level
            <select id="water_level_category">
              <option value="unknown">Unknown</option>
              <option value="full">Full</option>
              <option value="high">High</option>
              <option value="mid">Mid</option>
              <option value="low">Low</option>
              <option value="empty">Empty</option>
            </select>
          </label>
          <label>Water quality
            <select id="water_quality">
              <option value="unknown">Unknown</option>
              <option value="clear">Clear</option>
              <option value="normal">Normal</option>
              <option value="muddy">Muddy</option>
              <option value="algae">Algae</option>
              <option value="dark">Dark</option>
            </select>
          </label>
          <label>Water confidence
            <select id="water_confidence">
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Float / pipe condition
            <select id="float_pipe_condition">
              <option value="unknown">Unknown</option>
              <option value="normal">Normal</option>
              <option value="possibly_damaged">Possibly damaged</option>
              <option value="not_visible">Not visible</option>
            </select>
          </label>
          <label>Trough occlusion
            <select id="trough_occlusion_level">
              <option value="none">None</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="blocked">Blocked</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occluded by <input id="trough_occluded_by" type="text" maxlength="120"></label>
        </div>
      </div>
      <h3>Bale Slots</h3>
      <div class="bale-grid">
        <div class="bale-slot">
          <div class="slot-title">
            <h4>Bale 1</h4>
            <label><input id="bale_1_present" type="checkbox"> Present</label>
          </div>
          <label>Position
            <select id="bale_1_location">
              <option value="left">Left</option>
              <option value="middle">Middle</option>
              <option value="right">Right</option>
              <option value="far_left">Far left</option>
              <option value="far_right">Far right</option>
              <option value="background">Background</option>
              <option value="foreground">Foreground</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Remaining % <input id="bale_1_remaining_percent" type="number" min="0" max="100"></label>
          <label>Condition
            <select id="bale_1_condition">
              <option value="unknown">Unknown</option>
              <option value="new">New</option>
              <option value="mostly_full">Mostly full</option>
              <option value="half">Half</option>
              <option value="low">Low</option>
              <option value="collapsed">Collapsed</option>
              <option value="scattered">Mostly scattered</option>
              <option value="gone">Gone</option>
            </select>
          </label>
          <label>Color / quality
            <select id="bale_1_color_quality">
              <option value="normal">Normal</option>
              <option value="bright_fresh">Bright / fresh</option>
              <option value="dark_weathered">Dark / weathered</option>
              <option value="mixed">Mixed</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <div class="checks">
            <label><input id="bale_1_hay_ring_visible" type="checkbox"> Hay ring</label>
            <label><input id="bale_1_scatter_present" type="checkbox"> Scatter</label>
          </div>
          <label>Scatter level
            <select id="bale_1_scatter_level">
              <option value="none">None</option>
              <option value="trace">Trace</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Scatter bale equivalent <input id="bale_1_scatter_bale_equivalent" type="number" min="0" max="1" step="0.01"></label>
          <label>Slot visibility
            <select id="bale_1_visibility">
              <option value="clear">Clear</option>
              <option value="partly_occluded">Partly occluded</option>
              <option value="mostly_occluded">Mostly occluded</option>
              <option value="night_uncertain">Night uncertain</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Level confidence
            <select id="bale_1_level_confidence">
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion amount
            <select id="bale_1_occlusion_level">
              <option value="none">None</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="blocked">Blocked</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occluded by
            <select id="bale_1_occluded_by">
              <option value="none">None</option>
              <option value="cow">Cow</option>
              <option value="cattle_group">Cattle group</option>
              <option value="hay_ring">Hay ring</option>
              <option value="brush">Brush</option>
              <option value="shadow">Shadow</option>
              <option value="night">Night</option>
              <option value="terrain">Terrain / rise</option>
              <option value="equipment">Equipment</option>
              <option value="other">Other</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion note <input id="bale_1_occlusion_note" type="text" maxlength="140"></label>
        </div>
        <div class="bale-slot">
          <div class="slot-title">
            <h4>Bale 2</h4>
            <label><input id="bale_2_present" type="checkbox"> Present</label>
          </div>
          <label>Position
            <select id="bale_2_location">
              <option value="left">Left</option>
              <option value="middle">Middle</option>
              <option value="right">Right</option>
              <option value="far_left">Far left</option>
              <option value="far_right">Far right</option>
              <option value="background">Background</option>
              <option value="foreground">Foreground</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Remaining % <input id="bale_2_remaining_percent" type="number" min="0" max="100"></label>
          <label>Condition
            <select id="bale_2_condition">
              <option value="unknown">Unknown</option>
              <option value="new">New</option>
              <option value="mostly_full">Mostly full</option>
              <option value="half">Half</option>
              <option value="low">Low</option>
              <option value="collapsed">Collapsed</option>
              <option value="scattered">Mostly scattered</option>
              <option value="gone">Gone</option>
            </select>
          </label>
          <label>Color / quality
            <select id="bale_2_color_quality">
              <option value="normal">Normal</option>
              <option value="bright_fresh">Bright / fresh</option>
              <option value="dark_weathered">Dark / weathered</option>
              <option value="mixed">Mixed</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <div class="checks">
            <label><input id="bale_2_hay_ring_visible" type="checkbox"> Hay ring</label>
            <label><input id="bale_2_scatter_present" type="checkbox"> Scatter</label>
          </div>
          <label>Scatter level
            <select id="bale_2_scatter_level">
              <option value="none">None</option>
              <option value="trace">Trace</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Scatter bale equivalent <input id="bale_2_scatter_bale_equivalent" type="number" min="0" max="1" step="0.01"></label>
          <label>Slot visibility
            <select id="bale_2_visibility">
              <option value="clear">Clear</option>
              <option value="partly_occluded">Partly occluded</option>
              <option value="mostly_occluded">Mostly occluded</option>
              <option value="night_uncertain">Night uncertain</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Level confidence
            <select id="bale_2_level_confidence">
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion amount
            <select id="bale_2_occlusion_level">
              <option value="none">None</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="blocked">Blocked</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occluded by
            <select id="bale_2_occluded_by">
              <option value="none">None</option>
              <option value="cow">Cow</option>
              <option value="cattle_group">Cattle group</option>
              <option value="hay_ring">Hay ring</option>
              <option value="brush">Brush</option>
              <option value="shadow">Shadow</option>
              <option value="night">Night</option>
              <option value="terrain">Terrain / rise</option>
              <option value="equipment">Equipment</option>
              <option value="other">Other</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion note <input id="bale_2_occlusion_note" type="text" maxlength="140"></label>
        </div>
        <div class="bale-slot">
          <div class="slot-title">
            <h4>Bale 3</h4>
            <label><input id="bale_3_present" type="checkbox"> Present</label>
          </div>
          <label>Position
            <select id="bale_3_location">
              <option value="left">Left</option>
              <option value="middle">Middle</option>
              <option value="right">Right</option>
              <option value="far_left">Far left</option>
              <option value="far_right">Far right</option>
              <option value="background">Background</option>
              <option value="foreground">Foreground</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Remaining % <input id="bale_3_remaining_percent" type="number" min="0" max="100"></label>
          <label>Condition
            <select id="bale_3_condition">
              <option value="unknown">Unknown</option>
              <option value="new">New</option>
              <option value="mostly_full">Mostly full</option>
              <option value="half">Half</option>
              <option value="low">Low</option>
              <option value="collapsed">Collapsed</option>
              <option value="scattered">Mostly scattered</option>
              <option value="gone">Gone</option>
            </select>
          </label>
          <label>Color / quality
            <select id="bale_3_color_quality">
              <option value="normal">Normal</option>
              <option value="bright_fresh">Bright / fresh</option>
              <option value="dark_weathered">Dark / weathered</option>
              <option value="mixed">Mixed</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <div class="checks">
            <label><input id="bale_3_hay_ring_visible" type="checkbox"> Hay ring</label>
            <label><input id="bale_3_scatter_present" type="checkbox"> Scatter</label>
          </div>
          <label>Scatter level
            <select id="bale_3_scatter_level">
              <option value="none">None</option>
              <option value="trace">Trace</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Scatter bale equivalent <input id="bale_3_scatter_bale_equivalent" type="number" min="0" max="1" step="0.01"></label>
          <label>Slot visibility
            <select id="bale_3_visibility">
              <option value="clear">Clear</option>
              <option value="partly_occluded">Partly occluded</option>
              <option value="mostly_occluded">Mostly occluded</option>
              <option value="night_uncertain">Night uncertain</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Level confidence
            <select id="bale_3_level_confidence">
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion amount
            <select id="bale_3_occlusion_level">
              <option value="none">None</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="blocked">Blocked</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occluded by
            <select id="bale_3_occluded_by">
              <option value="none">None</option>
              <option value="cow">Cow</option>
              <option value="cattle_group">Cattle group</option>
              <option value="hay_ring">Hay ring</option>
              <option value="brush">Brush</option>
              <option value="shadow">Shadow</option>
              <option value="night">Night</option>
              <option value="terrain">Terrain / rise</option>
              <option value="equipment">Equipment</option>
              <option value="other">Other</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion note <input id="bale_3_occlusion_note" type="text" maxlength="140"></label>
        </div>
        <div class="bale-slot">
          <div class="slot-title">
            <h4>Bale 4</h4>
            <label><input id="bale_4_present" type="checkbox"> Present</label>
          </div>
          <label>Position
            <select id="bale_4_location">
              <option value="unknown">Unknown</option>
              <option value="left">Left</option>
              <option value="middle">Middle</option>
              <option value="right">Right</option>
              <option value="far_left">Far left</option>
              <option value="far_right">Far right</option>
              <option value="background">Background</option>
              <option value="foreground">Foreground</option>
              <option value="custom">Custom</option>
            </select>
          </label>
          <label>Remaining % <input id="bale_4_remaining_percent" type="number" min="0" max="100"></label>
          <label>Condition
            <select id="bale_4_condition">
              <option value="unknown">Unknown</option>
              <option value="new">New</option>
              <option value="mostly_full">Mostly full</option>
              <option value="half">Half</option>
              <option value="low">Low</option>
              <option value="collapsed">Collapsed</option>
              <option value="scattered">Mostly scattered</option>
              <option value="gone">Gone</option>
            </select>
          </label>
          <label>Color / quality
            <select id="bale_4_color_quality">
              <option value="normal">Normal</option>
              <option value="bright_fresh">Bright / fresh</option>
              <option value="dark_weathered">Dark / weathered</option>
              <option value="mixed">Mixed</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <div class="checks">
            <label><input id="bale_4_hay_ring_visible" type="checkbox"> Hay ring</label>
            <label><input id="bale_4_scatter_present" type="checkbox"> Scatter</label>
          </div>
          <label>Scatter level
            <select id="bale_4_scatter_level">
              <option value="none">None</option>
              <option value="trace">Trace</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Scatter bale equivalent <input id="bale_4_scatter_bale_equivalent" type="number" min="0" max="1" step="0.01"></label>
          <label>Slot visibility
            <select id="bale_4_visibility">
              <option value="clear">Clear</option>
              <option value="partly_occluded">Partly occluded</option>
              <option value="mostly_occluded">Mostly occluded</option>
              <option value="night_uncertain">Night uncertain</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Level confidence
            <select id="bale_4_level_confidence">
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion amount
            <select id="bale_4_occlusion_level">
              <option value="none">None</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="heavy">Heavy</option>
              <option value="blocked">Blocked</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occluded by
            <select id="bale_4_occluded_by">
              <option value="none">None</option>
              <option value="cow">Cow</option>
              <option value="cattle_group">Cattle group</option>
              <option value="hay_ring">Hay ring</option>
              <option value="brush">Brush</option>
              <option value="shadow">Shadow</option>
              <option value="night">Night</option>
              <option value="terrain">Terrain / rise</option>
              <option value="equipment">Equipment</option>
              <option value="other">Other</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Occlusion note <input id="bale_4_occlusion_note" type="text" maxlength="140"></label>
          <label>Position note <input id="bale_4_position_note" type="text" maxlength="120"></label>
        </div>
      </div>
      <h3>Scene Scatter / Residue</h3>
      <div class="checks">
        <label><input id="hay_scatter_present" type="checkbox"> Edible scatter visible</label>
      </div>
      <div class="grid2" style="margin-top:10px">
        <label>Scatter level
          <select id="hay_scatter_level">
            <option value="none">None</option>
            <option value="trace">Trace</option>
            <option value="light">Light</option>
            <option value="moderate">Moderate</option>
            <option value="heavy">Heavy</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
        <label>Scatter bale equivalent <input id="hay_scatter_bale_equivalent" type="number" min="0" max="1" step="0.01"></label>
      </div>
      <h3>Overall Hay Quality</h3>
      <div class="grid2">
        <label>Hay color / quality
          <select id="hay_color_quality">
            <option value="normal">Normal coloration</option>
            <option value="bright_fresh">Bright / fresh</option>
            <option value="dark_weathered">Dark / weathered</option>
            <option value="mixed">Mixed</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
      </div>
      <h3>Flags</h3>
      <div class="checks">
        <label><input id="new_bales_put_out" type="checkbox"> New bales put out</label>
        <label><input id="poor_visibility" type="checkbox"> Poor visibility</label>
      </div>
      <h3>Odd Sightings</h3>
      <div class="checks" id="odd_sightings">
        <label><input type="checkbox" value="person"> Person</label>
        <label><input type="checkbox" value="vehicle"> Vehicle</label>
        <label><input type="checkbox" value="deer"> Deer</label>
        <label><input type="checkbox" value="hog"> Hog</label>
        <label><input type="checkbox" value="equipment"> Equipment</label>
        <label><input type="checkbox" value="camera_blocked"> Camera blocked</label>
      </div>
      <div class="grid2" style="margin-top:12px">
        <label>Visibility
          <select id="visibility">
            <option value="clear">Clear</option>
            <option value="dim">Dim</option>
            <option value="night">Night</option>
            <option value="rain">Rain</option>
            <option value="blocked">Blocked</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
        <label>Label confidence
          <select id="label_confidence">
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
        </label>
      </div>
      <label style="margin-top:12px">Notes <textarea id="notes" rows="5" placeholder="Example: three fresh bales, cows not in frame, bale 2 mostly consumed"></textarea></label>
      <div class="actions">
        <button id="save_bottom" class="primary">Save Label</button>
        <button id="no_bales_save_bottom">No Bales + Save</button>
        <button id="clear">Clear Form</button>
      </div>
      <div class="status" id="status"></div>
    </section>
  </main>
  <script>
    const CAMERA_SLUG = __CAMERA_SLUG_JSON__;
    const API_BASE = __API_BASE_JSON__;
    const LABEL_SCHEMA_VERSION = __SCHEMA_VERSION_JSON__;
    const BALE_SLOT_COUNT = __BALE_SLOT_COUNT_JSON__;
    let images = [];
    let index = 0;
    const baleIds = Array.from({length: BALE_SLOT_COUNT}, (_, i) => i + 1);
    const baleFieldSuffixes = [
      'remaining_percent', 'location', 'condition', 'color_quality',
      'scatter_level', 'scatter_bale_equivalent', 'visibility',
      'level_confidence', 'occlusion_level', 'occluded_by', 'occlusion_note'
    ];
    const baleCheckSuffixes = ['present', 'hay_ring_visible', 'scatter_present'];
    const fields = [
      'round_bales_visible', 'bale_equivalents_remaining', 'hay_days_remaining', 'cattle_count',
      'cow_count', 'calf_count', 'bull_count',
      'horse_count', 'water_level_percent', 'water_level_category', 'water_quality',
      'water_confidence', 'float_pipe_condition', 'trough_occlusion_level', 'trough_occluded_by',
      ...baleIds.flatMap(slot => baleFieldSuffixes.map(suffix => `bale_${slot}_${suffix}`)),
      'bale_4_position_note',
      'hay_scatter_level', 'hay_scatter_bale_equivalent', 'hay_color_quality',
      'visibility', 'label_confidence', 'notes'
    ];
    const checks = [
      'no_bales_confirmed', 'cattle_present', 'new_bales_put_out', 'poor_visibility',
      'horse_present', 'longhorn_cow_present', 'water_trough_visible', 'water_visible',
      'float_pipe_visible', 'feed_tub_visible',
      ...baleIds.flatMap(slot => baleCheckSuffixes.map(suffix => `bale_${slot}_${suffix}`)),
      'hay_scatter_present'
    ];

    function $(id) { return document.getElementById(id); }
    function apiPath(path) { return `${API_BASE}${path}`; }
    function current() { return images[index]; }
    function fmtDate(value) {
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }
    function compact(value, suffix = '') {
      if (value === null || value === undefined || value === '') return '';
      const number = Number(value);
      const text = Number.isNaN(number) ? String(value) : String(Math.round(number * 10) / 10);
      return `${text}${suffix}`;
    }
    function setStatus(text) { $('status').textContent = text || ''; }

    function currentDraft() {
      const image = current();
      const intel = image?.hay_intelligence;
      return intel && intel.status === 'draft' && !image.label ? intel : null;
    }

    function updateActionState() {
      $('save_draft_top').disabled = !currentDraft();
    }

    function hayListText(intel) {
      if (!intel) return '';
      if (intel.no_bales_confirmed) return intel.status === 'human' ? 'Hay: no bales' : 'Draft: no bales';
      if (intel.bale_equivalents_remaining !== null && intel.bale_equivalents_remaining !== undefined) {
        return `${intel.status === 'human' ? 'Hay' : 'Draft'}: ${compact(intel.bale_equivalents_remaining)} bale eq`;
      }
      return intel.summary || '';
    }

    function renderHayIntelligence(intel) {
      if (!intel) return '';
      const statusText = intel.status === 'human' ? 'Human label' : intel.status === 'draft' ? 'Draft estimate' : 'Needs review';
      const chips = [];
      if (intel.no_bales_confirmed) chips.push('No bales');
      else if (intel.round_bales_visible !== null && intel.round_bales_visible !== undefined) chips.push(`${intel.round_bales_visible} bales`);
      if (intel.bale_equivalents_remaining !== null && intel.bale_equivalents_remaining !== undefined) chips.push(`${compact(intel.bale_equivalents_remaining)} bale eq`);
      if (intel.hay_days_remaining !== null && intel.hay_days_remaining !== undefined) chips.push(`${compact(intel.hay_days_remaining)} days`);
      if (intel.cattle_count) chips.push(`${intel.cattle_count} cattle`);
      if (intel.new_bales_put_out) chips.push('New bales');
      if (intel.confidence_score !== null && intel.confidence_score !== undefined) chips.push(`${Math.round(Number(intel.confidence_score) * 100)}% conf`);
      const apply = intel.status === 'draft'
        ? '<button id="apply_hay_intel" type="button">Use Draft + Save</button>'
        : '';
      return `<div class="hay-intel ${escapeHtml(intel.status || '')}">
        <div class="hay-intel-title"><span>Hay Intelligence</span><span>${escapeHtml(statusText)}</span></div>
        <div class="hay-intel-summary">${escapeHtml(intel.summary || 'Hay state needs review')}</div>
        <div class="hay-chips">${chips.map(chip => `<span class="hay-chip">${escapeHtml(chip)}</span>`).join('')}</div>
        <div class="hay-intel-basis">${escapeHtml(intel.basis || '')}${intel.nearest_label_hours !== undefined ? ` · nearest label ${escapeHtml(compact(intel.nearest_label_hours, 'h'))}` : ''}</div>
        ${apply}
      </div>`;
    }

    function applyRangePreset() {
      const value = $('range_preset').value;
      if (!value) return;
      const [start, end] = value.split(':');
      $('start').value = start;
      $('end').value = end;
    }

    async function loadImages() {
      applyRangePreset();
      const params = new URLSearchParams({
        start: $('start').value,
        end: $('end').value,
        limit: $('limit').value || '300',
        unlabeled: $('unlabeled').value
      });
      const response = await fetch(apiPath('/api/images') + '?' + params.toString());
      images = await response.json();
      index = 0;
      renderList();
      renderImage();
      setStatus(`${images.length} images loaded`);
    }

    function renderList() {
      $('list').innerHTML = images.map((image, i) => {
        const labeled = image.label ? '<span class="badge">labeled</span>' : '<span>unlabeled</span>';
        const mode = image.image_mode === 'source' ? '<span>source queue</span>' : '<span>TOPHAND</span>';
        const hay = hayListText(image.hay_intelligence);
        return `<div class="item ${i === index ? 'active' : ''}" data-index="${i}">
          <img src="${image.public_url}" alt="">
          <div><strong>${fmtDate(image.captured_at)}</strong><span>${image.temperature_text || ''} ${labeled}</span>${mode}<div class="item-hay">${escapeHtml(hay)}</div></div>
        </div>`;
      }).join('');
      document.querySelectorAll('.item').forEach(node => {
        node.addEventListener('click', () => {
          index = Number(node.dataset.index);
          renderList();
          renderImage();
        });
      });
    }

    function clearForm() {
      fields.forEach(id => { $(id).value = ''; });
      $('visibility').value = 'clear';
      $('label_confidence').value = 'high';
      $('hay_scatter_level').value = 'none';
      $('hay_color_quality').value = 'normal';
      $('water_level_category').value = 'unknown';
      $('water_quality').value = 'unknown';
      $('water_confidence').value = 'high';
      $('float_pipe_condition').value = 'unknown';
      $('trough_occlusion_level').value = 'none';
      baleIds.forEach(slot => {
        $(`bale_${slot}_location`).value = slot === 1 ? 'left' : slot === 2 ? 'middle' : slot === 3 ? 'right' : 'unknown';
        $(`bale_${slot}_condition`).value = 'unknown';
        $(`bale_${slot}_color_quality`).value = 'normal';
        $(`bale_${slot}_scatter_level`).value = 'none';
        $(`bale_${slot}_visibility`).value = 'clear';
        $(`bale_${slot}_level_confidence`).value = 'high';
        $(`bale_${slot}_occlusion_level`).value = 'none';
        $(`bale_${slot}_occluded_by`).value = 'none';
      });
      checks.forEach(id => { $(id).checked = false; });
      document.querySelectorAll('#odd_sightings input').forEach(node => { node.checked = false; });
      updateDerivedFields();
      updateNoBalesState();
    }

    function loadLabel(label) {
      clearForm();
      if (!label) return;
      fields.forEach(id => {
        if (label[id] !== undefined && label[id] !== null) $(id).value = label[id];
      });
      checks.forEach(id => { $(id).checked = Boolean(label[id]); });
      (label.bale_slots || []).forEach(slotLabel => {
        const slot = Number(slotLabel.slot);
        if (!baleIds.includes(slot)) return;
        const mappings = {
          present: 'present',
          location: 'location',
          remaining_percent: 'remaining_percent',
          condition: 'condition',
          color_quality: 'color_quality',
          hay_ring_visible: 'hay_ring_visible',
          scatter_present: 'scatter_present',
          scatter_level: 'scatter_level',
          scatter_bale_equivalent: 'scatter_bale_equivalent',
          visibility: 'visibility',
          level_confidence: 'level_confidence',
          occlusion_level: 'occlusion_level',
          occluded_by: 'occluded_by',
          occlusion_note: 'occlusion_note'
        };
        Object.entries(mappings).forEach(([source, suffix]) => {
          const id = `bale_${slot}_${suffix}`;
          if ($(id) && slotLabel[source] !== undefined && slotLabel[source] !== null) {
            if ($(id).type === 'checkbox') $(id).checked = Boolean(slotLabel[source]);
            else $(id).value = slotLabel[source];
          }
        });
        if (slot === 4 && slotLabel.position_note) $('bale_4_position_note').value = slotLabel.position_note;
      });
      const odd = new Set(label.odd_sightings || []);
      document.querySelectorAll('#odd_sightings input').forEach(node => { node.checked = odd.has(node.value); });
      updateDerivedFields();
      updateNoBalesState();
    }

    function renderImage() {
      const image = current();
      if (!image) {
        $('image').removeAttribute('src');
        $('meta').textContent = 'No images loaded';
        $('hay_intel').innerHTML = '';
        clearForm();
        updateActionState();
        return;
      }
      $('image').src = image.public_url;
      $('image').alt = image.path;
      const mode = image.image_mode === 'source' ? 'raw source' : 'TOPHAND branded';
      const range = image.queue_range ? `<span>${image.queue_range}</span>` : '';
      $('meta').innerHTML = `<strong>${fmtDate(image.captured_at)}</strong><span>${image.temperature_text || ''}</span><span>${mode}</span>${range}<span>${escapeHtml(image.path)}</span>`;
      $('hay_intel').innerHTML = renderHayIntelligence(image.hay_intelligence);
      const apply = $('apply_hay_intel');
      if (apply) apply.addEventListener('click', () => saveDraft());
      if (image.label) {
        loadLabel(image.label);
      } else if (image.hay_intelligence?.status === 'draft') {
        applyHayIntelligence(image.hay_intelligence, {silent: true});
      } else {
        clearForm();
      }
      updateActionState();
    }

    function applyHayIntelligence(intel, options = {}) {
      if (!intel) return;
      clearForm();
      $('no_bales_confirmed').checked = Boolean(intel.no_bales_confirmed);
      if (intel.round_bales_visible !== null && intel.round_bales_visible !== undefined) $('round_bales_visible').value = intel.round_bales_visible;
      if (intel.bale_equivalents_remaining !== null && intel.bale_equivalents_remaining !== undefined) $('bale_equivalents_remaining').value = intel.bale_equivalents_remaining;
      if (intel.hay_days_remaining !== null && intel.hay_days_remaining !== undefined) $('hay_days_remaining').value = intel.hay_days_remaining;
      $('cattle_present').checked = Boolean(intel.cattle_present);
      if (intel.cattle_count !== null && intel.cattle_count !== undefined) $('cattle_count').value = intel.cattle_count || '';
      if (intel.cow_count !== null && intel.cow_count !== undefined) $('cow_count').value = intel.cow_count || '';
      if (intel.calf_count !== null && intel.calf_count !== undefined) $('calf_count').value = intel.calf_count || '';
      if (intel.bull_count !== null && intel.bull_count !== undefined) $('bull_count').value = intel.bull_count || '';
      $('new_bales_put_out').checked = Boolean(intel.new_bales_put_out);
      (intel.bale_slots || []).forEach(slotIntel => {
        const slot = Number(slotIntel.slot);
        if (!baleIds.includes(slot)) return;
        const mappings = {
          present: 'present',
          location: 'location',
          remaining_percent: 'remaining_percent',
          condition: 'condition',
          color_quality: 'color_quality',
          hay_ring_visible: 'hay_ring_visible',
          scatter_present: 'scatter_present',
          scatter_level: 'scatter_level',
          visibility: 'visibility',
          level_confidence: 'level_confidence',
          occlusion_level: 'occlusion_level',
          occluded_by: 'occluded_by'
        };
        Object.entries(mappings).forEach(([source, suffix]) => {
          const id = `bale_${slot}_${suffix}`;
          if (!$(id) || slotIntel[source] === undefined || slotIntel[source] === null) return;
          if ($(id).type === 'checkbox') $(id).checked = Boolean(slotIntel[source]);
          else $(id).value = slotIntel[source];
        });
      });
      $('notes').value = `Draft hay intelligence applied. ${intel.summary || ''}`.trim();
      updateDerivedFields();
      updateNoBalesState();
      if (!options.silent) setStatus('Draft applied. Review the image before saving.');
    }

    function numberValue(id) {
      const value = $(id).value;
      return value === '' ? null : Number(value);
    }

    function textValue(id) {
      const value = $(id).value.trim();
      return value === '' ? null : value;
    }

    function formHasHayData() {
      return $('no_bales_confirmed').checked
        || numberValue('round_bales_visible') !== null
        || numberValue('bale_equivalents_remaining') !== null
        || baleIds.some(slot => numberValue(`bale_${slot}_remaining_percent`) !== null || $(`bale_${slot}_present`).checked);
    }

    function animalTotal() {
      return ['cow_count', 'calf_count', 'bull_count'].reduce((total, id) => total + (numberValue(id) || 0), 0);
    }

    function updateDerivedFields() {
      const total = animalTotal();
      $('cattle_count').value = total || '';
      if (total > 0) $('cattle_present').checked = true;
      if ((numberValue('horse_count') || 0) > 0) $('horse_present').checked = true;
    }

    function updateNoBalesState() {
      const noBales = $('no_bales_confirmed').checked;
      const baleFields = [
        'round_bales_visible', 'bale_equivalents_remaining',
        ...baleIds.flatMap(slot => [
          `bale_${slot}_present`,
          `bale_${slot}_remaining_percent`,
          `bale_${slot}_location`,
          `bale_${slot}_condition`,
          `bale_${slot}_color_quality`,
          `bale_${slot}_hay_ring_visible`,
          `bale_${slot}_scatter_present`,
          `bale_${slot}_scatter_level`,
          `bale_${slot}_scatter_bale_equivalent`,
          `bale_${slot}_visibility`,
          `bale_${slot}_level_confidence`,
          `bale_${slot}_occlusion_level`,
          `bale_${slot}_occluded_by`,
          `bale_${slot}_occlusion_note`
        ]),
        'bale_4_position_note'
      ];
      if (noBales) {
        $('round_bales_visible').value = 0;
        $('bale_equivalents_remaining').value = 0;
        baleIds.forEach(slot => { $(`bale_${slot}_remaining_percent`).value = 0; });
        baleIds.forEach(slot => {
          $(`bale_${slot}_present`).checked = false;
          $(`bale_${slot}_hay_ring_visible`).checked = false;
          $(`bale_${slot}_scatter_present`).checked = false;
          $(`bale_${slot}_scatter_level`).value = 'none';
          $(`bale_${slot}_scatter_bale_equivalent`).value = '';
          $(`bale_${slot}_occlusion_level`).value = 'none';
          $(`bale_${slot}_occluded_by`).value = 'none';
          $(`bale_${slot}_occlusion_note`).value = '';
        });
      }
      baleFields.forEach(id => {
        if (id === 'round_bales_visible') return;
        $(id).disabled = noBales;
      });
    }

    function updateBaleSlotState(slot) {
      if (numberValue(`bale_${slot}_remaining_percent`) !== null) $(`bale_${slot}_present`).checked = true;
      const scatterLevel = $(`bale_${slot}_scatter_level`).value;
      if (scatterLevel && scatterLevel !== 'none') $(`bale_${slot}_scatter_present`).checked = true;
      if ((numberValue(`bale_${slot}_scatter_bale_equivalent`) || 0) > 0) $(`bale_${slot}_scatter_present`).checked = true;
      if ($(`bale_${slot}_present`).checked) $('no_bales_confirmed').checked = false;
      updateNoBalesState();
    }

    function baleSlot(slot) {
      const present = $('no_bales_confirmed').checked
        ? false
        : ($(`bale_${slot}_present`).checked || numberValue(`bale_${slot}_remaining_percent`) !== null);
      return {
        slot,
        present,
        location: $(`bale_${slot}_location`).value,
        remaining_percent: numberValue(`bale_${slot}_remaining_percent`),
        condition: $(`bale_${slot}_condition`).value,
        color_quality: $(`bale_${slot}_color_quality`).value,
        hay_ring_visible: $(`bale_${slot}_hay_ring_visible`).checked,
        scatter_present: $(`bale_${slot}_scatter_present`).checked,
        scatter_level: $(`bale_${slot}_scatter_level`).value,
        scatter_bale_equivalent: numberValue(`bale_${slot}_scatter_bale_equivalent`),
        visibility: $(`bale_${slot}_visibility`).value,
        level_confidence: $(`bale_${slot}_level_confidence`).value,
        occlusion_level: $(`bale_${slot}_occlusion_level`).value,
        occluded_by: $(`bale_${slot}_occluded_by`).value,
        occlusion_note: textValue(`bale_${slot}_occlusion_note`),
        position_note: slot === 4 ? textValue('bale_4_position_note') : null
      };
    }

    function baleFlatFields(slots) {
      const flat = {};
      slots.forEach(slotData => {
        const prefix = `bale_${slotData.slot}`;
        flat[`${prefix}_present`] = slotData.present;
        flat[`${prefix}_location`] = slotData.location;
        flat[`${prefix}_remaining_percent`] = slotData.remaining_percent;
        flat[`${prefix}_condition`] = slotData.condition;
        flat[`${prefix}_color_quality`] = slotData.color_quality;
        flat[`${prefix}_hay_ring_visible`] = slotData.hay_ring_visible;
        flat[`${prefix}_scatter_present`] = slotData.scatter_present;
        flat[`${prefix}_scatter_level`] = slotData.scatter_level;
        flat[`${prefix}_scatter_bale_equivalent`] = slotData.scatter_bale_equivalent;
        flat[`${prefix}_visibility`] = slotData.visibility;
        flat[`${prefix}_level_confidence`] = slotData.level_confidence;
        flat[`${prefix}_occlusion_level`] = slotData.occlusion_level;
        flat[`${prefix}_occluded_by`] = slotData.occluded_by;
        flat[`${prefix}_occlusion_note`] = slotData.occlusion_note;
      });
      flat.bale_4_position_note = textValue('bale_4_position_note');
      return flat;
    }

    function buildPayload() {
      const image = current();
      const odd = [...document.querySelectorAll('#odd_sightings input:checked')].map(node => node.value);
      const baleSlots = baleIds.map(slot => baleSlot(slot));
      const labelPath = image.source_path || image.path;
      updateDerivedFields();
      return {
        schema_version: LABEL_SCHEMA_VERSION,
        path: labelPath,
        display_path: image.path,
        branded_path: image.image_mode === 'branded' ? image.path : null,
        image_mode: image.image_mode || 'branded',
        source_path: image.source_path || null,
        device: image.device,
        camera_title: image.camera_title,
        captured_at: image.captured_at,
        temperature_text: image.temperature_text || null,
        no_bales_confirmed: $('no_bales_confirmed').checked,
        round_bales_visible: numberValue('round_bales_visible'),
        ...baleFlatFields(baleSlots),
        bale_slots: baleSlots,
        bales: baleSlots,
        bale_equivalents_remaining: numberValue('bale_equivalents_remaining'),
        hay_days_remaining: numberValue('hay_days_remaining'),
        hay_scatter_present: $('hay_scatter_present').checked,
        hay_scatter_level: $('hay_scatter_level').value,
        hay_scatter_bale_equivalent: numberValue('hay_scatter_bale_equivalent'),
        hay_color_quality: $('hay_color_quality').value,
        cattle_present: $('cattle_present').checked,
        cattle_count: numberValue('cattle_count'),
        cow_count: numberValue('cow_count'),
        calf_count: numberValue('calf_count'),
        bull_count: numberValue('bull_count'),
        horse_present: $('horse_present').checked,
        horse_count: numberValue('horse_count'),
        longhorn_cow_present: $('longhorn_cow_present').checked,
        water_trough_visible: $('water_trough_visible').checked,
        water_visible: $('water_visible').checked,
        water_level_percent: numberValue('water_level_percent'),
        water_level_category: $('water_level_category').value,
        water_quality: $('water_quality').value,
        water_confidence: $('water_confidence').value,
        float_pipe_visible: $('float_pipe_visible').checked,
        float_pipe_condition: $('float_pipe_condition').value,
        trough_occlusion_level: $('trough_occlusion_level').value,
        trough_occluded_by: textValue('trough_occluded_by'),
        feed_tub_visible: $('feed_tub_visible').checked,
        new_bales_put_out: $('new_bales_put_out').checked,
        poor_visibility: $('poor_visibility').checked,
        odd_sightings: odd,
        visibility: $('visibility').value,
        label_confidence: $('label_confidence').value,
        notes: $('notes').value.trim()
      };
    }

    function markSavedIntelligence(saved) {
      const image = current();
      if (!image) return;
      const baleEq = saved.bale_equivalents_remaining;
      let summary = 'Saved human label';
      if (saved.no_bales_confirmed) {
        summary = 'No bales confirmed';
      } else if (baleEq !== null && baleEq !== undefined) {
        summary = `${saved.round_bales_visible || 0} bales, about ${baleEq} bale equivalents`;
      }
      image.hay_intelligence = {
        ...(image.hay_intelligence || {}),
        status: 'human',
        analysis_source: 'human_label',
        basis: 'saved in labeler',
        summary,
        no_bales_confirmed: Boolean(saved.no_bales_confirmed),
        round_bales_visible: saved.round_bales_visible,
        bale_equivalents_remaining: saved.bale_equivalents_remaining,
        hay_days_remaining: saved.hay_days_remaining,
        cattle_present: Boolean(saved.cattle_present),
        cattle_count: saved.cattle_count,
        new_bales_put_out: Boolean(saved.new_bales_put_out),
        confidence_score: 1
      };
    }

    async function saveLabel(statusText = 'Saved') {
      if (!current()) return;
      const response = await fetch(apiPath('/api/label'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(buildPayload())
      });
      if (!response.ok) {
        setStatus(await response.text());
        return;
      }
      const saved = await response.json();
      images[index].label = saved;
      markSavedIntelligence(saved);
      renderList();
      $('hay_intel').innerHTML = renderHayIntelligence(images[index].hay_intelligence);
      updateActionState();
      setStatus(statusText);
    }

    async function saveDraft() {
      const draft = currentDraft();
      if (!draft) {
        setStatus('No draft available for this image.');
        return;
      }
      if (!formHasHayData()) applyHayIntelligence(draft, {silent: true});
      await saveLabel('Draft saved as reviewed label');
    }

    function setNoBalesFields() {
      clearForm();
      $('no_bales_confirmed').checked = true;
      $('round_bales_visible').value = 0;
      $('bale_equivalents_remaining').value = 0;
      $('hay_days_remaining').value = 0;
      updateNoBalesState();
    }

    async function noBalesAndSave() {
      if (!current()) return;
      setNoBalesFields();
      await saveLabel('No bales saved');
    }

    $('load').addEventListener('click', loadImages);
    $('range_preset').addEventListener('change', () => { applyRangePreset(); loadImages(); });
    $('save_top').addEventListener('click', () => saveLabel());
    $('save_bottom').addEventListener('click', () => saveLabel());
    $('save_draft_top').addEventListener('click', saveDraft);
    $('no_bales_save_top').addEventListener('click', noBalesAndSave);
    $('no_bales_save_bottom').addEventListener('click', noBalesAndSave);
    $('clear').addEventListener('click', clearForm);
    $('no_bales_confirmed').addEventListener('change', updateNoBalesState);
    ['cow_count', 'calf_count', 'bull_count', 'horse_count'].forEach(id => $(id).addEventListener('input', updateDerivedFields));
    baleIds.forEach(slot => {
      $(`bale_${slot}_present`).addEventListener('change', () => updateBaleSlotState(slot));
      $(`bale_${slot}_remaining_percent`).addEventListener('input', () => updateBaleSlotState(slot));
      $(`bale_${slot}_scatter_level`).addEventListener('change', () => updateBaleSlotState(slot));
      $(`bale_${slot}_scatter_bale_equivalent`).addEventListener('input', () => updateBaleSlotState(slot));
    });
    $('prev').addEventListener('click', () => { if (index > 0) { index -= 1; renderList(); renderImage(); } });
    $('next').addEventListener('click', () => { if (index < images.length - 1) { index += 1; renderList(); renderImage(); } });
    document.addEventListener('keydown', event => {
      if (event.key === 'ArrowLeft') $('prev').click();
      if (event.key === 'ArrowRight') $('next').click();
      if ((event.metaKey || event.ctrlKey) && event.key === 's') { event.preventDefault(); saveLabel(); }
    });
    loadImages();
  </script>
</body>
</html>"""
    return (
        page.replace("__PAGE_TITLE__", html.escape(config.page_title))
        .replace("__PAGE_SUBTITLE__", html.escape(config.subtitle))
        .replace("__CAMERA_NAV__", nav_html(config.slug, configs))
        .replace("__RANGE_OPTIONS__", range_options(config))
        .replace("__BODY_CLASSES__", html.escape(" ".join(body_classes)))
        .replace("__WATER_SECTION_TITLE__", html.escape(config.water_section_title or "Water Source"))
        .replace("__CAMERA_SLUG__", html.escape(config.slug))
        .replace("__CAMERA_SLUG_JSON__", json.dumps(config.slug))
        .replace("__API_BASE_JSON__", json.dumps(config.route_path))
        .replace("__SCHEMA_VERSION_JSON__", json.dumps(config.schema_version))
        .replace("__BALE_SLOT_COUNT_JSON__", json.dumps(config.bale_slot_count))
    )


class Handler(BaseHTTPRequestHandler):
    indexes: dict[str, ImageIndex]
    labels_by_slug: dict[str, LabelStore]
    configs: dict[str, CameraConfig]

    def resolve_camera(self, path: str) -> tuple[str, str]:
        for slug, config in self.configs.items():
            if not config.route_path:
                continue
            if path == config.route_path or path.startswith(config.route_path + "/"):
                route = path[len(config.route_path) :] or "/"
                return slug, route
        return "pastucha-hay", path

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text: str, status: int = HTTPStatus.OK, content_type: str = "text/plain") -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        slug, route = self.resolve_camera(parsed.path)
        if route == "/":
            self.send_text(html_page(self.configs[slug], self.configs), content_type="text/html")
            return
        if route == "/api/images":
            params = parse_qs(parsed.query)
            self.send_json(self.indexes[slug].query(params, self.labels_by_slug[slug]))
            return
        if route == "/api/reload":
            self.indexes[slug].reload()
            self.send_json({"ok": True, "count": len(self.indexes[slug].images)})
            return
        self.send_text(f"Not found: {html.escape(parsed.path)}", status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        slug, route = self.resolve_camera(parsed.path)
        if route != "/api/label":
            self.send_text("Not found", status=HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            saved = self.labels_by_slug[slug].upsert(payload)
        except Exception as exc:  # noqa: BLE001
            self.send_text(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        self.send_json(saved)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    args = parse_args()
    branding.load_env_file(args.env)
    client = branding.SupabaseRest(
        branding.require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
        branding.require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    )
    configs = camera_configs(args.data_root, args.data_dir, args.source_queue)
    for config in configs.values():
        config.data_dir.mkdir(parents=True, exist_ok=True)
    Handler.configs = configs
    Handler.indexes = {
        slug: ImageIndex(client, config, args.manifest_path, args.source_bucket)
        for slug, config in configs.items()
    }
    Handler.labels_by_slug = {
        slug: LabelStore(config.data_dir, config.schema_version)
        for slug, config in configs.items()
    }
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Pastucha Hay labeler: http://{args.host}:{args.port}/")
    for slug, config in configs.items():
        url_path = config.route_path or "/"
        print(f"{config.camera_title}: http://{args.host}:{args.port}{url_path}")
        print(f"  Images indexed: {len(Handler.indexes[slug].images)}")
        print(f"  Data dir: {config.data_dir}")
        print(f"  Source queue: {config.source_queue_path}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
