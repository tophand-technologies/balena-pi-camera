#!/usr/bin/env python3
"""Evaluate Pastucha Hay VLM prompt/model candidates against golden labels."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

import tophand_branding_worker as branding


DEFAULT_RESEARCH_DIR = Path("/home/travis/tophand-instances/sdco/research/pastucha-hay")
DEFAULT_LABELS = DEFAULT_RESEARCH_DIR / "golden_labels.latest.json"
CAMERA_ID = "FLEX-M-MGE4"


PROMPTS = {
    "hay_strict_json": """You are analyzing the Pastucha Hay ranch camera.
This camera watches the round-bale feeding area for SDCO cattle.
Ignore the TOPHAND black overlay bar. Use the scene only.

Return strict JSON only:
{
  "no_bales_confirmed": boolean,
  "round_bales_visible": integer,
  "bales": [
    {
      "slot": integer,
      "location": "left|middle|right|far_left|far_right|background|foreground|unknown",
      "present": boolean,
      "remaining_percent": integer 0-100,
      "condition": "new|mostly_full|half|low|collapsed|scattered|gone|unknown",
      "color_quality": "normal|bright_fresh|dark_weathered|mixed|unknown",
      "hay_ring_visible": boolean,
      "scatter_present": boolean,
      "scatter_level": "none|trace|light|moderate|heavy|unknown",
      "scatter_bale_equivalent": number 0-1,
      "visibility": "clear|partly_occluded|mostly_occluded|night_uncertain|unknown",
      "level_confidence": "high|medium|low|unknown",
      "occlusion_level": "none|light|moderate|heavy|blocked|unknown",
      "occluded_by": "none|cow|cattle_group|hay_ring|brush|shadow|night|terrain|equipment|other|unknown",
      "occlusion_note": "short note or null"
    }
  ],
  "bale_equivalents_remaining": number,
  "hay_days_remaining": number|null,
  "cattle_present": boolean,
  "cattle_count": integer,
  "cow_count": integer,
  "calf_count": integer,
  "bull_count": integer,
  "hay_scatter_present": boolean,
  "hay_scatter_level": "none|trace|light|moderate|heavy|unknown",
  "hay_scatter_bale_equivalent": number 0-1,
  "hay_color_quality": "normal|bright_fresh|dark_weathered|mixed|unknown",
  "new_bales_put_out": boolean,
  "odd_sightings": array of "person|vehicle|deer|hog|equipment|camera_blocked",
  "visibility": "clear|dim|night|rain|blocked|unknown",
  "confidence_score": number 0-1,
  "notes": "short factual note"
}

If you cannot see hay bales, use 0 and explain briefly in notes.""",
    "ranch_hand_estimator": """Think like an experienced ranch hand checking a hay feeding area.
Estimate the number of round bales, how much of each remains, whether cattle are
present, and whether anything unusual is in the frame. Ignore timestamps and
printed overlay text.

Return only valid JSON with:
no_bales_confirmed, round_bales_visible, bales, bale_equivalents_remaining,
hay_days_remaining, cattle_present, cattle_count, cow_count, calf_count,
bull_count, hay_scatter_present, hay_scatter_level,
hay_scatter_bale_equivalent, hay_color_quality, new_bales_put_out,
odd_sightings, visibility, confidence_score, notes.

For bale equivalents, one untouched round bale is 1.0. A half-eaten bale is 0.5.
Track left/middle/right bale slots separately, including hay ring visibility,
hay color/quality, edible scatter around each bale, and whether the slot is
occluded or uncertain. If an animal blocks a bale but you can still confidently
infer the bale level, report the bale level and separately record occlusion
level, occluded_by, and level_confidence.
Do not hallucinate bales hidden outside the frame.""",
    "two_step_observe_decide": """Inspect this Pastucha Hay camera image in two steps, but output only final JSON.
Step 1: observe visible round bales, cattle, people/vehicles/wildlife, visibility.
Step 2: decide bale count, remaining percent per bale, bale equivalents, and hay days.
Ignore the TOPHAND overlay bar.

Return strict JSON:
{
  "no_bales_confirmed": boolean,
  "round_bales_visible": integer,
  "bales": [{
    "slot": integer,
    "location": string,
    "present": boolean,
    "remaining_percent": integer,
    "condition": string,
    "color_quality": string,
    "hay_ring_visible": boolean,
    "scatter_present": boolean,
    "scatter_level": string,
    "scatter_bale_equivalent": number,
    "visibility": string,
    "level_confidence": string,
    "occlusion_level": string,
    "occluded_by": string,
    "occlusion_note": string|null
  }],
  "bale_equivalents_remaining": number,
  "hay_days_remaining": number|null,
  "cattle_present": boolean,
  "cattle_count": integer,
  "cow_count": integer,
  "calf_count": integer,
  "bull_count": integer,
  "hay_scatter_present": boolean,
  "hay_scatter_level": string,
  "hay_scatter_bale_equivalent": number,
  "hay_color_quality": string,
  "new_bales_put_out": boolean,
  "odd_sightings": array,
  "visibility": string,
  "confidence_score": number,
  "notes": string
}""",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Pastucha Hay prompt/model research.")
    parser.add_argument("--env", type=Path, default=Path("/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env"))
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    parser.add_argument("--bucket", default=branding.DEST_BUCKET)
    parser.add_argument("--source-bucket", default=branding.SOURCE_BUCKET)
    parser.add_argument("--models", nargs="+", default=["qwen2.5vl:32b", "qwen3-vl:latest", "gemma4:31b"])
    parser.add_argument("--prompts", nargs="+", default=list(PROMPTS))
    parser.add_argument("--views", nargs="+", default=["full", "hay_zone"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--vlm-timeout", type=int, default=240)
    parser.add_argument("--max-width", type=int, default=1280)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_labels(path: Path, limit: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise branding.WorkerError(f"Expected object in {path}")
    latest_by_source: dict[str, dict[str, Any]] = {}
    for value in data.values():
        if not isinstance(value, dict) or value.get("device") != CAMERA_ID:
            continue
        identity = str(value.get("source_path") or value.get("path") or "")
        if not identity:
            continue
        existing = latest_by_source.get(identity)
        if existing is None:
            latest_by_source[identity] = value
            continue
        value_time = branding.parse_sort_time(value.get("updated_at") or value.get("captured_at"))
        existing_time = branding.parse_sort_time(existing.get("updated_at") or existing.get("captured_at"))
        if value_time >= existing_time:
            latest_by_source[identity] = value
    rows = list(latest_by_source.values())
    rows.sort(key=lambda row: branding.parse_sort_time(row.get("captured_at")), reverse=True)
    return rows[:limit]


def label_storage_ref(label: dict[str, Any], args: argparse.Namespace) -> tuple[str, str]:
    source_path = label.get("source_path")
    if source_path:
        return args.source_bucket, str(source_path)
    path = str(label.get("path") or "")
    if label.get("image_mode") == "source":
        return args.source_bucket, path
    return args.bucket, path


def image_to_view_bytes(image_bytes: bytes, view: str, max_width: int) -> bytes:
    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size

    # Remove the branded overlay. It is useful for capture time, not hay research.
    image = image.crop((0, 0, width, max(1, round(height * 0.89))))

    if view == "hay_zone":
        # Pastucha Hay bales are typically in the central/open feeding area.
        left = round(image.width * 0.05)
        right = round(image.width * 0.95)
        top = round(image.height * 0.08)
        bottom = round(image.height * 0.92)
        image = image.crop((left, top, right, bottom))

    if image.width > max_width:
        scale = max_width / image.width
        image = image.resize((max_width, max(1, round(image.height * scale))), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def call_ollama(ollama_url: str, model: str, prompt: str, image_bytes: bytes, timeout: int) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0},
    }
    response = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout)
    data = branding.api_json(response)
    return (data or {}).get("response", "")


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def number(value: Any, fallback: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return fallback
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(result) or math.isinf(result):
        return fallback
    return result


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "present"}
    return bool(value)


def listish(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,;/]", str(value))
    return {str(item).strip().lower() for item in values if str(item).strip()}


def label_bale_equivalents(label: dict[str, Any]) -> float:
    explicit = label.get("bale_equivalents_remaining")
    if explicit is not None:
        return number(explicit)
    total = 0.0
    for index in range(1, 5):
        total += number(label.get(f"bale_{index}_remaining_percent")) / 100.0
    return round(total, 2)


def prediction_bale_equivalents(prediction: dict[str, Any]) -> float:
    explicit = prediction.get("bale_equivalents_remaining")
    if explicit is not None:
        return number(explicit)
    bales = prediction.get("bales") or []
    total = 0.0
    if isinstance(bales, list):
        for bale in bales:
            if isinstance(bale, dict):
                total += number(bale.get("remaining_percent")) / 100.0
    return round(total, 2)


def animal_count(row: dict[str, Any]) -> float:
    explicit = row.get("cattle_count")
    if explicit is not None:
        return number(explicit)
    return number(row.get("cow_count")) + number(row.get("calf_count")) + number(row.get("bull_count"))


def score_prediction(label: dict[str, Any], prediction: dict[str, Any], valid_json: bool) -> dict[str, Any]:
    if not valid_json:
        return {"score": 999.0, "invalid_json": 1}

    label_odd = listish(label.get("odd_sightings"))
    pred_odd = listish(prediction.get("odd_sightings"))
    odd_false_negatives = len(label_odd - pred_odd)
    odd_false_positives = len(pred_odd - label_odd)

    bale_count_error = abs(number(label.get("round_bales_visible")) - number(prediction.get("round_bales_visible")))
    bale_equiv_error = abs(label_bale_equivalents(label) - prediction_bale_equivalents(prediction))
    hay_days_error = abs(number(label.get("hay_days_remaining")) - number(prediction.get("hay_days_remaining")))
    cattle_count_error = abs(animal_count(label) - animal_count(prediction))
    cow_count_error = abs(number(label.get("cow_count")) - number(prediction.get("cow_count")))
    calf_count_error = abs(number(label.get("calf_count")) - number(prediction.get("calf_count")))
    bull_count_error = abs(number(label.get("bull_count")) - number(prediction.get("bull_count")))
    cattle_present_error = 0 if boolish(label.get("cattle_present")) == boolish(prediction.get("cattle_present")) else 1
    no_bales_error = 0 if boolish(label.get("no_bales_confirmed")) == boolish(prediction.get("no_bales_confirmed")) else 1
    new_bales_error = 0 if boolish(label.get("new_bales_put_out")) == boolish(prediction.get("new_bales_put_out")) else 1

    score = (
        bale_count_error * 4.0
        + bale_equiv_error * 4.0
        + min(hay_days_error, 7) * 1.5
        + min(cattle_count_error, 20) * 0.5
        + min(cow_count_error + calf_count_error + bull_count_error, 20) * 0.35
        + cattle_present_error * 2.0
        + no_bales_error * 2.0
        + new_bales_error * 3.0
        + odd_false_negatives * 3.0
        + odd_false_positives * 1.5
    )
    return {
        "score": round(score, 3),
        "invalid_json": 0,
        "bale_count_error": bale_count_error,
        "bale_equiv_error": round(bale_equiv_error, 3),
        "hay_days_error": hay_days_error,
        "cattle_count_error": cattle_count_error,
        "cow_count_error": cow_count_error,
        "calf_count_error": calf_count_error,
        "bull_count_error": bull_count_error,
        "cattle_present_error": cattle_present_error,
        "no_bales_error": no_bales_error,
        "new_bales_error": new_bales_error,
        "odd_false_negatives": odd_false_negatives,
        "odd_false_positives": odd_false_positives,
    }


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["model"], row["prompt_name"], row["view"])
        grouped.setdefault(key, []).append(row["score"])

    summaries = []
    for (model, prompt_name, view), scores in grouped.items():
        count = len(scores)
        summaries.append(
            {
                "model": model,
                "prompt_name": prompt_name,
                "view": view,
                "count": count,
                "mean_score": round(sum(item["score"] for item in scores) / count, 3),
                "invalid_json_rate": round(sum(item.get("invalid_json", 0) for item in scores) / count, 3),
                "mean_bale_count_error": round(sum(item.get("bale_count_error", 0) for item in scores) / count, 3),
                "mean_bale_equiv_error": round(sum(item.get("bale_equiv_error", 0) for item in scores) / count, 3),
            }
        )
    summaries.sort(key=lambda row: row["mean_score"])
    return summaries


def main() -> int:
    args = parse_args()
    if requests is None:
        raise branding.WorkerError("Install requests before running AutoResearch.")

    branding.load_env_file(args.env)
    args.ollama_url = branding.normalize_ollama_url(
        args.ollama_url or os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST")
    )

    labels = load_labels(args.labels, args.limit)
    if not labels:
        raise branding.WorkerError(f"No labels found in {args.labels}")

    client = branding.SupabaseRest(
        branding.require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
        branding.require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    )
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.research_dir / "candidate_outputs"
    eval_dir = args.research_dir / "eval_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{run_id}.jsonl"
    eval_path = eval_dir / f"{run_id}.json"

    rows: list[dict[str, Any]] = []
    total = len(labels) * len(args.models) * len(args.prompts) * len(args.views)
    print(f"AutoResearch queued {total} trials from {len(labels)} labels")

    with output_path.open("a", encoding="utf-8") as handle:
        trial = 0
        for label in labels:
            image_bucket, image_path = label_storage_ref(label, args)
            image_bytes = client.download(image_bucket, image_path)
            view_bytes = {view: image_to_view_bytes(image_bytes, view, args.max_width) for view in args.views}
            for model in args.models:
                for prompt_name in args.prompts:
                    prompt = PROMPTS[prompt_name]
                    for view in args.views:
                        trial += 1
                        started = time.time()
                        raw_text = ""
                        prediction: dict[str, Any] = {}
                        error = None
                        if args.dry_run:
                            error = "dry_run"
                        else:
                            try:
                                raw_text = call_ollama(args.ollama_url, model, prompt, view_bytes[view], args.vlm_timeout)
                                prediction = extract_json(raw_text)
                            except Exception as exc:  # noqa: BLE001
                                error = str(exc)
                        valid_json = bool(prediction)
                        score = score_prediction(label, prediction, valid_json)
                        row = {
                            "run_id": run_id,
                            "trial": trial,
                            "model": model,
                            "prompt_name": prompt_name,
                            "view": view,
                            "path": label["path"],
                            "image_bucket": image_bucket,
                            "image_path": image_path,
                            "source_path": label.get("source_path"),
                            "captured_at": label.get("captured_at"),
                            "seconds": round(time.time() - started, 2),
                            "prediction": prediction,
                            "raw_response": raw_text[:4000],
                            "error": error,
                            "score": score,
                        }
                        rows.append(row)
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
                        print(
                            f"[{trial}/{total}] {model} {prompt_name} {view}: "
                            f"score={score['score']} invalid={score['invalid_json']}"
                        )

    summary = {
        "run_id": run_id,
        "labels": len(labels),
        "models": args.models,
        "prompts": args.prompts,
        "views": args.views,
        "output_path": str(output_path),
        "rankings": summarize(rows),
    }
    eval_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Eval written: {eval_path}")
    print(json.dumps(summary["rankings"][:5], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
