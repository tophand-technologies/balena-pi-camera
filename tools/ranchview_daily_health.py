#!/usr/bin/env python3
"""Daily RanchView health and learning-flywheel report.

The report intentionally separates source upload freshness from capture truth:
source object dates are only used to find recent backlog, while branded gallery
dates come from the image overlay extraction saved in the TOPHAND manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import tophand_branding_worker as branding


DEFAULT_INSTANCE_DIR = Path("/home/travis/tophand-instances/sdco")
INTEL_TAGS = ["water_trough", "water_pond", "cattle", "horse", "person", "vehicle", "deer", "hog"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check RanchView gallery and training flywheel health.")
    parser.add_argument("--env", type=Path, default=DEFAULT_INSTANCE_DIR / ".secrets/dtzay-supabase.env")
    parser.add_argument("--instance-dir", type=Path, default=DEFAULT_INSTANCE_DIR)
    parser.add_argument("--source-bucket", default=branding.SOURCE_BUCKET)
    parser.add_argument("--dest-bucket", default=branding.DEST_BUCKET)
    parser.add_argument("--manifest-path", default="manifest.json")
    parser.add_argument("--source-limit", type=int, default=1000)
    parser.add_argument("--recent-limit", type=int, default=200)
    parser.add_argument("--min-bytes", type=int, default=10_000)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def age_hours(value: Any) -> float | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    return round((utc_now() - parsed.astimezone(dt.UTC)).total_seconds() / 3600, 2)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def count_labels(path: Path) -> dict[str, Any]:
    payload = load_json(path, {})
    if isinstance(payload, dict):
        labels = list(payload.values())
    elif isinstance(payload, list):
        labels = payload
    else:
        labels = []

    updated_times = [parse_time(row.get("updated_at") or row.get("labeled_at")) for row in labels if isinstance(row, dict)]
    captured_times = [parse_time(row.get("captured_at")) for row in labels if isinstance(row, dict)]
    updated_times = [value for value in updated_times if value]
    captured_times = [value for value in captured_times if value]
    camera_titles = Counter(str(row.get("camera_title") or row.get("device") or "unknown") for row in labels if isinstance(row, dict))
    odd_counts = Counter()
    for row in labels:
        if not isinstance(row, dict):
            continue
        for item in row.get("odd_sightings") or []:
            odd_counts[str(item)] += 1

    return {
        "path": str(path),
        "count": len(labels),
        "latest_updated_at": max(updated_times).isoformat() if updated_times else None,
        "latest_updated_age_hours": age_hours(max(updated_times).isoformat()) if updated_times else None,
        "latest_capture_at": max(captured_times).isoformat() if captured_times else None,
        "camera_titles": dict(camera_titles),
        "odd_sightings": dict(odd_counts),
    }


def eval_summary(research_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(research_dir.glob("*/eval_results/*.json")):
        payload = load_json(path, {})
        rankings = payload.get("rankings") if isinstance(payload, dict) else None
        best = None
        if isinstance(rankings, list) and rankings:
            best = sorted(rankings, key=lambda row: float(row.get("mean_score", 999999)))[0]
        runs.append(
            {
                "path": str(path),
                "run_id": payload.get("run_id"),
                "labels": payload.get("labels"),
                "best": best,
                "updated_at": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).isoformat(),
            }
        )
    return runs[-5:]


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    images = manifest.get("images") or []
    tag_counts = Counter()
    analysis_model_counts = Counter()
    by_camera: dict[str, dict[str, Any]] = defaultdict(lambda: {"branded": 0, "analysis": 0, "latest_capture_at": None})
    analyzed = 0
    for image in images:
        device = image.get("device") or "unknown"
        camera = by_camera[device]
        camera["camera_title"] = image.get("camera_title") or device
        camera["branded"] += 1
        captured = parse_time(image.get("captured_at"))
        if captured and (not camera["latest_capture_at"] or captured > parse_time(camera["latest_capture_at"])):
            camera["latest_capture_at"] = captured.isoformat()

        analysis = image.get("analysis") or {}
        if isinstance(analysis, dict) and analysis:
            analyzed += 1
            camera["analysis"] += 1
            if analysis.get("analysis_model"):
                analysis_model_counts[str(analysis["analysis_model"])] += 1
            for tag in set(analysis.get("filter_tags") or []):
                if tag in INTEL_TAGS:
                    tag_counts[tag] += 1

    captured_times = [parse_time(image.get("captured_at")) for image in images]
    captured_times = [value for value in captured_times if value]
    return {
        "generated_at": manifest.get("generated_at"),
        "generated_age_hours": age_hours(manifest.get("generated_at")),
        "count": len(images),
        "analysis_rows": analyzed,
        "missing_analysis": max(0, len(images) - analyzed),
        "newest_capture_at": max(captured_times).isoformat() if captured_times else None,
        "oldest_capture_at": min(captured_times).isoformat() if captured_times else None,
        "tag_counts": {tag: tag_counts.get(tag, 0) for tag in INTEL_TAGS},
        "analysis_model_counts": dict(analysis_model_counts),
        "by_camera": dict(sorted(by_camera.items())),
    }


def source_backlog(
    client: branding.SupabaseRest,
    source_bucket: str,
    dest_bucket: str,
    source_limit: int,
    recent_limit: int,
    min_bytes: int,
) -> dict[str, Any]:
    sources = branding.list_source_objects(client, source_bucket, source_limit, min_bytes, None)
    existing = branding.manifest_source_paths(client, dest_bucket)
    recent_sources = sources[:recent_limit]
    unbranded = [source for source in recent_sources if source.path not in existing]
    by_camera = Counter(source.device for source in unbranded)
    source_by_camera = Counter(source.device for source in recent_sources)
    latest_source = sources[0] if sources else None

    return {
        "source_limit": source_limit,
        "recent_limit": recent_limit,
        "source_images_seen": len(sources),
        "recent_source_images_seen": len(recent_sources),
        "existing_manifest_sources": len(existing),
        "recent_unbranded_count": len(unbranded),
        "recent_unbranded_by_camera": dict(by_camera),
        "recent_source_by_camera": dict(source_by_camera),
        "latest_source_upload_at": latest_source.created_at if latest_source else None,
        "latest_source_upload_age_hours": age_hours(latest_source.created_at) if latest_source else None,
        "sample_recent_unbranded": [
            {"device": source.device, "created_at": source.created_at, "path": source.path}
            for source in unbranded[:10]
        ],
    }


def status_and_actions(report: dict[str, Any]) -> tuple[str, list[str]]:
    actions: list[str] = []
    manifest = report["manifest"]
    backlog = report["source_backlog"]
    training = report["training"]

    if backlog["recent_unbranded_count"] > 25:
        actions.append(
            f"Run a bounded TOPHAND branding catch-up; {backlog['recent_unbranded_count']} of the "
            f"latest {backlog['recent_limit']} source images are not in the branded manifest."
        )
    if manifest["missing_analysis"] > 0:
        actions.append(f"Run VLM enrichment for {manifest['missing_analysis']} branded images missing analysis.")
    if manifest["tag_counts"].get("hog", 0) == 0:
        actions.append("Keep the hog filter active but backfill/search older camera ranges; no hog hits are in the current manifest.")
    if training["label_total"] < 50:
        actions.append(f"Add more human-reviewed labels; current golden-label total is {training['label_total']}.")
    if training["eval_runs"]:
        latest_eval = training["eval_runs"][-1]
        eval_labels = int(latest_eval.get("labels") or 0)
        if training["label_total"] - eval_labels >= 5:
            actions.append(
                f"Rerun AutoResearch; golden labels have grown by {training['label_total'] - eval_labels} "
                "since the latest eval."
            )
        best = latest_eval.get("best") or {}
        invalid_json_rate = best.get("invalid_json_rate")
        if isinstance(invalid_json_rate, (int, float)) and invalid_json_rate >= 0.2:
            actions.append(
                f"Fix or replace the current AutoResearch prompt/model candidate; latest invalid JSON rate is "
                f"{invalid_json_rate:.2f}."
            )
    stale_label_sets = [
        item["slug"]
        for item in training["label_sets"]
        if item.get("latest_updated_age_hours") is not None and item["latest_updated_age_hours"] > 168
    ]
    if stale_label_sets:
        actions.append("Review fresh training labels for stale sets: " + ", ".join(stale_label_sets) + ".")
    if not training["eval_runs"]:
        actions.append("Run AutoResearch once enough labels exist for each camera schema.")

    if not actions:
        return "green", ["No immediate action; gallery and training flywheel are current."]
    if backlog["recent_unbranded_count"] > 75 or manifest["missing_analysis"] > 25:
        return "red", actions
    return "yellow", actions


def build_markdown(report: dict[str, Any]) -> str:
    manifest = report["manifest"]
    backlog = report["source_backlog"]
    training = report["training"]
    deltas = report.get("deltas") or {}

    lines = [
        f"# RanchView Daily Health - {report['generated_at']}",
        "",
        f"Status: {report['status'].upper()}",
        "",
        "## Gallery",
        f"- TOPHAND branded images: {manifest['count']} ({deltas.get('manifest_count', 0):+})",
        f"- VLM analysis rows: {manifest['analysis_rows']} ({deltas.get('analysis_rows', 0):+})",
        f"- Missing analysis: {manifest['missing_analysis']}",
        f"- Manifest generated age: {manifest['generated_age_hours']} hours",
        f"- Newest overlay capture: {manifest['newest_capture_at']}",
        f"- Recent unbranded source backlog: {backlog['recent_unbranded_count']} of {backlog['recent_limit']}",
        "",
        "## Intelligence Tags",
    ]
    for tag, count in manifest["tag_counts"].items():
        delta = (deltas.get("tag_counts") or {}).get(tag, 0)
        lines.append(f"- {tag}: {count} ({delta:+})")

    lines.extend(["", "## Training"])
    lines.append(f"- Golden labels: {training['label_total']} ({deltas.get('label_total', 0):+})")
    for item in training["label_sets"]:
        lines.append(
            f"- {item['slug']}: {item['count']} labels, latest update {item.get('latest_updated_at') or 'unknown'}"
        )
    if training["eval_runs"]:
        latest_eval = training["eval_runs"][-1]
        best = latest_eval.get("best") or {}
        lines.append(
            f"- Latest eval: {latest_eval.get('run_id')} on {latest_eval.get('labels')} labels, "
            f"best score {best.get('mean_score', 'unknown')}"
        )
    else:
        lines.append("- Latest eval: none found")

    lines.extend(["", "## Actions"])
    for action in report["actions"]:
        lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)


def add_deltas(report: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not previous:
        report["deltas"] = {}
        return
    old_manifest = previous.get("manifest") or {}
    old_training = previous.get("training") or {}
    old_tags = old_manifest.get("tag_counts") or {}
    new_tags = report["manifest"]["tag_counts"]
    report["deltas"] = {
        "manifest_count": report["manifest"]["count"] - int(old_manifest.get("count") or 0),
        "analysis_rows": report["manifest"]["analysis_rows"] - int(old_manifest.get("analysis_rows") or 0),
        "label_total": report["training"]["label_total"] - int(old_training.get("label_total") or 0),
        "tag_counts": {tag: new_tags.get(tag, 0) - int(old_tags.get(tag) or 0) for tag in INTEL_TAGS},
    }


def main() -> int:
    args = parse_args()
    branding.load_env_file(args.env)
    client = branding.SupabaseRest(
        branding.require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
        branding.require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    )

    manifest = client.download_json_optional(args.dest_bucket, args.manifest_path)
    if not manifest:
        raise branding.WorkerError(f"Could not load {args.dest_bucket}/{args.manifest_path}")

    output_dir = args.output_dir or args.instance_dir / "research/ranchview-health"
    previous = load_json(output_dir / "latest.json", None)
    research_dir = args.instance_dir / "research"
    label_sets = []
    for path in sorted(research_dir.glob("*/golden_labels.latest.json")):
        item = count_labels(path)
        item["slug"] = path.parent.name
        label_sets.append(item)

    report = {
        "generated_at": utc_now().isoformat(),
        "manifest": manifest_summary(manifest),
        "source_backlog": source_backlog(
            client,
            args.source_bucket,
            args.dest_bucket,
            args.source_limit,
            args.recent_limit,
            args.min_bytes,
        ),
        "training": {
            "label_total": sum(item["count"] for item in label_sets),
            "label_sets": label_sets,
            "eval_runs": eval_summary(research_dir),
        },
    }
    add_deltas(report, previous)
    report["status"], report["actions"] = status_and_actions(report)
    markdown = build_markdown(report)
    report["markdown_path"] = str(output_dir / "latest.md")

    if not args.no_write:
        day = utc_now().astimezone(branding.CAPTURE_TZ).strftime("%Y-%m-%d")
        write_json(output_dir / "daily" / f"{day}.json", report)
        write_json(output_dir / "latest.json", report)
        (output_dir / "latest.md").parent.mkdir(parents=True, exist_ok=True)
        (output_dir / "latest.md").write_text(markdown, encoding="utf-8")

    print(markdown)
    return 1 if report["status"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
