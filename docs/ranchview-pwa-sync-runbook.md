# RanchView PWA Image Sync Runbook

Canonical operations doc:
`docs/RANCHVIEW_IMAGE_GALLERY_OPERATIONS.md`.

## Purpose

`balena-pi-camera.vercel.app` is a static Vercel PWA. The Refresh button only
reloads the Supabase bucket configured in `spypoint-viewer.html`; it does not
run a server-side sync.

## Pipeline

```text
SpyPoint cameras
  -> RanchEye sync on 5090
  -> RanchEye Supabase project enoyydytzcgejwmivshz, bucket spypoint-images
  -> ranchview-dtzay-publisher on 5090
  -> PWA Supabase project dtzayqhebbrbvordmabh, bucket spypoint-images
  -> balena-pi-camera.vercel.app
```

## Ownership

- PWA repo: `C:\Users\TravisEtzler\Documents\GitHub\balena-pi-camera`
- PWA host: Vercel project `balena-pi-camera`
- PWA bucket: `dtzayqhebbrbvordmabh`, bucket `spypoint-images`
- Source server: 5090, `travis@100.66.5.91`
- Source API: `http://localhost:8000/api/images` on 5090
- Source env: `/home/travis/rancheye-unified/.env`
- Destination env: `/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env`
- Publisher script on 5090: `/home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py`
- Publisher report: `/home/travis/tophand-instances/sdco/research/ranchview-dtzay-publisher.jsonl`
- Systemd units:
  - `ranchview-dtzay-publisher.service`
  - `ranchview-dtzay-publisher.timer`
- Installed state on 5090: timer enabled and active as of 2026-05-27.

## 2026-05-27 Recovery Record

- The PWA was still reading `dtzay/spypoint-images` correctly. New test objects
  uploaded to that bucket appeared in the fullscreen PWA.
- RanchEye on 5090 was still syncing fresh source images into the `enoy`
  Supabase project.
- No boot-resilient `dtzay` publisher was found in systemd, cron, or Supabase
  Edge Functions. The closest existing RanchEye/OpenClaw paths feed the current
  RanchEye project, not the PWA bucket.
- Backfill command used:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py --since 2026-05-20 --limit 2000 --write --report /home/travis/tophand-instances/sdco/research/ranchview-dtzay-publisher.jsonl"
```

- Backfill result: 574 candidates from RanchEye API, 574 present in `dtzay`,
  0 failures after one timeout retry.
- Follow-up May 18 audit: 574 candidates, 574 already present, 0 uploads,
  0 failures.
- Verification after install: timer enabled/active; one service run processed
  260 recent candidates, all already present, 0 failures.
- Hardening follow-up: health check detected 24 fresh RanchEye objects that
  arrived after the previous publisher run; publisher copied them into `dtzay`;
  final health check passed with 10 latest public PWA objects returning OK.

## Safety Rules

- Do not publish directly from SpyPoint thumbnail URLs.
- `_S_` filenames are allowed because the historical dtzay feed used them, but
  only as destination names. Never download bytes from `thumbnail_url`.
- Source bytes must come from the RanchEye Supabase storage object.
- The publisher rejects files smaller than 5000 bytes.
- The destination path is flat by camera: `{camera}/{PICT...YYYYMMDDHHMM....jpg}`.

## Check Freshness

RanchEye source:

```bash
ssh travis@100.66.5.91 "python3 - <<'PY'
import requests, re
r = requests.get('http://localhost:8000/api/images', params={'limit': 5, 'days_back': 10}, timeout=20)
r.raise_for_status()
for img in r.json().get('images', []):
    hd = (img.get('metadata') or {}).get('hd_url') or ''
    name = re.search(r'PICT[^/?]+', hd)
    print(img.get('captured_at'), img.get('camera_name'), name.group(0) if name else 'no-hd-name')
PY"
```

Publisher status:

```bash
ssh travis@100.66.5.91 "systemctl status ranchview-dtzay-publisher.timer --no-pager"
ssh travis@100.66.5.91 "journalctl -u ranchview-dtzay-publisher.service -n 80 --no-pager"
```

Manual dry-run:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py --since-days 3 --limit 25"
```

Manual publish:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py --since-days 3 --limit 1000 --write"
```

Backfill after outage:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py --since 2026-05-20 --limit 2000 --write"
```

## Restart Behavior

The timer is configured with `Persistent=true`, `OnBootSec=3min`, and
`OnUnitActiveSec=15min`. After a shutdown or reboot, systemd should run the
publisher shortly after network/Docker startup, then every 15 minutes.

If RanchEye is up but the PWA remains stale, check the publisher journal first.
