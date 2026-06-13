# Supabase Configuration

## Project Details

**Project:** ranch-view
**Project ID:** dtzayqhebbrbvordmabh
**API URL:** https://dtzayqhebbrbvordmabh.supabase.co

## Storage Bucket

**Bucket Name:** `spypoint-images` (PUBLIC bucket)
**Camera Folder:** `tophand-zero-04`

### Upload Path Structure

Images are uploaded to:
```
spypoint-images/
  └── tophand-zero-04/
      └── YYYY/
          └── MM/
              └── DD/
                  └── tophand-zero-04_YYYYMMDD_HHMMSS.jpg
```

Example: `spypoint-images/tophand-zero-04/2026/03/08/tophand-zero-04_20260308_143022.jpg`

## API Credentials

Stored in 1Password and injected at runtime. Do not commit raw Supabase API keys.

- **SUPABASE_URL:** `https://dtzayqhebbrbvordmabh.supabase.co`
- **SUPABASE_SECRET_KEY:** server-side key for Pi/runtime upload jobs.
- **TOPHAND_SUPABASE_PUBLISHABLE_KEY:** browser-safe publishable key for static viewers.
- **SUPABASE_BUCKET:** `spypoint-images`

1Password items:

- Server runtime: `op://TH-Provider-API-Prod/svc:supabase:ranch-view:server-secret-key:prod/credential`
- Browser viewers: `op://TH-Provider-API-Prod/svc:supabase:ranch-view:browser-publishable-key:prod/credential`

Runtime files:

- `ranch-camera.service` loads `/etc/tophand/ranch-camera.env` for `SUPABASE_SECRET_KEY` or transitional `SUPABASE_KEY`.
- Browser viewers optionally load gitignored `supabase-browser-config.local.js` before `supabase-browser-config.js`.
- Use `supabase-browser-config.local.example.js` as the non-secret shape for browser runtime injection.
- Vercel serves `/supabase-browser-config.local.js` from `api/supabase/browser-config.js`, backed by the Vercel env var `TOPHAND_SUPABASE_PUBLISHABLE_KEY`.

Vercel rollout target:

- Team: `tophand projects`
- Project: `balena-pi-camera`
- Env var name: `TOPHAND_SUPABASE_PUBLISHABLE_KEY`
- Env var value source: `op://TH-Provider-API-Prod/gfhehuyai7yyxdmjiwu6sulngi/credential`
- Apply to: production first; preview/development only if those deployments also need direct browser Supabase access.

Pause rule:

- Do not deploy `vercel.json`/API route changes until the Vercel env var exists. Without the env var, the remote browser config endpoint will not provide the publishable key.

## S3 API Access (Alternative)

If needed, Supabase Storage also supports S3 protocol:

- **S3 Endpoint:** `https://dtzayqhebbrbvordmabh.storage.supabase.co/storage/v1/s3`
- **Region:** `us-west-2`

## Deployment

The Supabase configuration is deployed via runtime env/config injection:

```bash
./deploy_updates.sh
```

Do not copy credentials into committed service files. The Pi should receive a protected `/etc/tophand/ranch-camera.env` from 1Password-backed deployment.

## Testing Upload

After deployment, test the upload:

```bash
# Trigger a test capture
ssh pi@10.42.0.1 'sudo systemctl start ranch-camera.service'

# Check upload logs
ssh pi@10.42.0.1 'journalctl -u ranch-camera.service -n 20'
```

## Viewing Uploaded Images

Images are accessible via Supabase Storage dashboard:

1. Go to https://supabase.com/dashboard/project/dtzayqhebbrbvordmabh
2. Navigate to Storage → spypoint-images
3. Open folder: tophand-zero-04/YYYY/MM/DD/

Since the bucket is PUBLIC, images can also be accessed via direct URL:
```
https://dtzayqhebbrbvordmabh.supabase.co/storage/v1/object/public/spypoint-images/tophand-zero-04/2026/03/08/filename.jpg
```

## Image Upload Details

- **High Quality (HQ):** ~820KB saved to SD card (`/home/pi/camera/archive/`)
- **Compressed:** ~118KB uploaded to Supabase via cellular
- **Format:** JPEG, 2304x1296, quality=10 for cellular upload
- **Rotation:** 180° applied (camera mounted upside down)
