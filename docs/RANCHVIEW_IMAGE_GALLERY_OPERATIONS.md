# RanchView Image Gallery Operations

Last verified: 2026-05-27

## What Must Stay Running

The fullscreen PWA at `balena-pi-camera.vercel.app` is a static Vercel app. It
does not fetch images from SpyPoint directly and it does not run a backend sync
when Refresh Images is pressed. Refresh Images only reloads the public Supabase
bucket configured in `spypoint-viewer.html`.

Live dependency chain:

```text
SpyPoint cameras / SpyPoint cloud
  -> 5090 RanchEye sync
  -> RanchEye Supabase project enoyydytzcgejwmivshz, bucket spypoint-images
  -> ranchview-dtzay-publisher on 5090
  -> PWA Supabase project dtzayqhebbrbvordmabh, bucket spypoint-images
  -> Vercel PWA balena-pi-camera.vercel.app
```

Only 5090 is required for this image-gallery refresh path. 5070 is not part of
the live image feed.

## Source Of Truth

- Repo: `C:\Users\TravisEtzler\Documents\GitHub\balena-pi-camera`
- Vercel project: `balena-pi-camera`
- PWA entry page: `spypoint-viewer.html`
- Source server: `ssh travis@100.66.5.91`
- Source API on 5090: `http://localhost:8000/api/images`
- Source env on 5090: `/home/travis/rancheye-unified/.env`
- Destination env on 5090: `/home/travis/tophand-instances/sdco/.secrets/dtzay-supabase.env`
- Publisher script on 5090: `/home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py`
- Health check script on 5090: `/home/travis/tophand-instances/sdco/tools/ranchview_pwa_healthcheck.py`
- Publisher report: `/home/travis/tophand-instances/sdco/research/ranchview-dtzay-publisher.jsonl`
- Health status file: `/home/travis/tophand-instances/sdco/research/ranchview-pwa-healthcheck.json`

## Systemd Services

These units must exist on 5090:

- `ranchview-dtzay-publisher.service`
- `ranchview-dtzay-publisher.timer`
- `ranchview-pwa-healthcheck.service`
- `ranchview-pwa-healthcheck.timer`

Expected timer behavior:

- Publisher starts 3 minutes after boot and runs every 15 minutes.
- Health check starts 5 minutes after boot and runs every 30 minutes.
- The health-check service asks systemd to run the publisher first, then checks
  whether the PWA bucket has the latest expected public objects.
- Both timers use `Persistent=true`, so missed runs are caught after downtime.

## One Command Health Check

Run this from any machine with SSH access to 5090:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_pwa_healthcheck.py"
```

Healthy means:

- RanchEye API responds.
- RanchEye has recent source images.
- Publisher timer is enabled and active.
- Publisher service last result is success.
- The expected PWA object paths for the latest RanchEye images return public
  HTTP 200 from the `dtzay` Supabase bucket.
- Public image byte sizes are above the tiny-image rejection threshold.

Check the latest machine-readable status:

```bash
ssh travis@100.66.5.91 "cat /home/travis/tophand-instances/sdco/research/ranchview-pwa-healthcheck.json"
```

## Normal Care

Weekly:

```bash
ssh travis@100.66.5.91 "systemctl --no-pager list-timers ranchview-dtzay-publisher.timer ranchview-pwa-healthcheck.timer"
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_pwa_healthcheck.py"
```

Before planned shutdown:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_pwa_healthcheck.py"
```

After restart:

```bash
ssh travis@100.66.5.91 "systemctl is-active ranchview-dtzay-publisher.timer ranchview-pwa-healthcheck.timer"
ssh travis@100.66.5.91 "sudo systemctl start ranchview-dtzay-publisher.service && sudo systemctl start ranchview-pwa-healthcheck.service"
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_pwa_healthcheck.py"
```

## Manual Backfill

Use this only when the PWA has missed images:

```bash
ssh travis@100.66.5.91 "python3 /home/travis/tophand-instances/sdco/tools/ranchview_dtzay_publisher.py --since 2026-05-20 --limit 2000 --write --report /home/travis/tophand-instances/sdco/research/ranchview-dtzay-publisher.jsonl"
```

Change the `--since` date to the first local date that might be missing.

## Safety Rules

- Do not download bytes from SpyPoint `thumbnail_url`.
- Do not publish tiny direct SpyPoint crop URLs into the PWA bucket.
- `_S_` filenames are allowed only as destination names because the historical
  PWA feed used that naming pattern.
- Source bytes must come from RanchEye Supabase storage.
- Keep destination paths flat by camera:
  `{camera}/{PICT..._S_YYYYMMDDHHMM....jpg}`.
- Do not store service-role keys in docs, git output, or chat.

## May 2026 Lesson

The May 19-27 outage was not a Vercel app failure. The PWA kept reading the
`dtzay` bucket correctly. RanchEye also kept syncing fresh source images. The
missing part was the explicit bridge that copies fresh RanchEye Supabase images
into the older PWA Supabase bucket.

The old bridge was not found in systemd, cron, or Supabase Edge Functions. The
repair made the bridge explicit, tracked in this repo, installed on 5090, and
covered by systemd timers plus a health check.

## 2026-05-27 Hardening Verification

- Publisher timer installed, enabled, and active.
- Health-check timer installed, enabled, and active.
- Health-check service is ordered after the publisher service so timed checks
  publish first, then verify public PWA objects.
- First health run caught 24 fresh RanchEye images that had arrived after the
  prior publisher run. A publisher run uploaded those 24 images into `dtzay`.
- Final direct health check result: OK, 10 latest public PWA objects checked,
  0 failed checks.

## Simplification Roadmap

Best future simplification:

```text
SpyPoint cameras / SpyPoint cloud
  -> 5090 RanchEye sync
  -> RanchEye Supabase project enoyydytzcgejwmivshz
  -> Vercel PWA
```

That removes the `dtzay` copy bridge entirely. To do that safely:

1. Update the PWA to read directly from the RanchEye Supabase project.
2. Preserve the filename/date sorting behavior the current PWA expects.
3. Verify fullscreen PWA behavior on desktop and mobile.
4. Keep the old `dtzay` bucket read path available until the new path has run
   clean for at least one week.
5. Remove the publisher timer only after the direct RanchEye path is proven.

Until that simplification is done, the current hardened design is the canonical
operating model.
