#!/usr/bin/env python3
"""Publish Pastucha Hay intelligence into TOPHAND branded sidecars."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pastucha_hay_labeler as hay
import tophand_branding_worker as branding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Pastucha Hay hay intelligence to branded metadata.")
    parser.add_argument("--env", type=Path, default=Path("/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env"))
    parser.add_argument("--data-dir", type=Path, default=hay.DEFAULT_DATA_DIR)
    parser.add_argument("--bucket", default=branding.DEST_BUCKET)
    parser.add_argument("--manifest-path", default="manifest.json")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def merge_analysis(metadata: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    analysis = metadata.get("analysis") or metadata.get("ranch_eye_analysis") or {}
    if not isinstance(analysis, dict):
        analysis = {"summary": str(analysis)}
    analysis["hay"] = intelligence
    analysis["hay_intelligence"] = intelligence
    if not analysis.get("summary"):
        analysis["summary"] = intelligence.get("summary")
    metadata["analysis"] = analysis
    return metadata


def main() -> int:
    args = parse_args()
    branding.load_env_file(args.env)
    client = branding.SupabaseRest(
        branding.require_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
        branding.require_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    )
    labels = hay.LabelStore(args.data_dir)
    manifest = client.download_json_optional(args.bucket, args.manifest_path)
    if not manifest:
        raise branding.WorkerError(f"Could not load {args.bucket}/{args.manifest_path}")

    updated = 0
    dry_run = 0
    for image in manifest.get("images", []):
        if image.get("device") != hay.CAMERA_ID:
            continue
        label = labels.get(image.get("source_path"), image.get("path"))
        intelligence = labels.hay_intelligence(image, label)
        metadata_path = branding.branded_metadata_path(image["path"])
        metadata = client.download_json_optional(args.bucket, metadata_path) or {}
        metadata = merge_analysis(metadata, intelligence)
        if args.write:
            client.upload_bytes(
                args.bucket,
                metadata_path,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8"),
                "application/json",
            )
            updated += 1
        else:
            dry_run += 1
        print(f"{image.get('captured_at')} {intelligence.get('status')}: {intelligence.get('summary')}")

    if args.write:
        count = branding.publish_manifest(client, args.bucket, args.limit)
        print(f"Manifest updated: {count} branded images")
    print(json.dumps({"updated": updated, "dry_run": dry_run}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
