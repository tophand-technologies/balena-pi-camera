#!/usr/bin/env python3
"""Inspect Supabase tables/buckets without printing secrets."""

from __future__ import annotations

import os
from pathlib import Path

from supabase import create_client


def load_env(path: Path) -> None:
    for line in path.read_text(errors="ignore").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    env_path = Path(".env")
    if env_path.exists():
        load_env(env_path)

    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    client = create_client(url, key)

    for table in ["images", "spypoint_images"]:
        try:
            result = client.table(table).select("*").limit(1).execute()
            print("TABLE", table, "rows", len(result.data))
            if result.data:
                print("columns", sorted(result.data[0].keys()))
        except Exception as exc:
            print("ERR", table, str(exc)[:300])

    try:
        buckets = client.storage.list_buckets()
        names = []
        for bucket in buckets:
            names.append(
                getattr(bucket, "name", None)
                or (bucket.get("name") if isinstance(bucket, dict) else str(bucket))
            )
        print("BUCKETS", names)
    except Exception as exc:
        print("BUCKET_ERR", exc)


if __name__ == "__main__":
    main()
