# cronappwriteabuhg17

This repository syncs a Supabase Storage bucket into GitHub on a schedule.

## What it does

- Runs a GitHub Actions workflow every hour
- Downloads files from the configured Supabase Storage bucket
- Stores the downloaded files under `synced/<bucket>/`
- Writes a manifest file to `synced/<bucket>-manifest.json`
- Commits and pushes changes back to this repository automatically

## Required GitHub Secrets

Add these repository secrets in GitHub:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_BUCKET`

For your current setup:

- `SUPABASE_URL=https://jqkjoqqellhsrhhzlkvr.supabase.co`
- `SUPABASE_BUCKET=abuhg17`

Keep the anon key in GitHub Secrets only. Do not commit it into the repository.

## Workflow

The workflow file is:

- `.github/workflows/hourly-sync.yml`

It runs on:

- every hour at minute `0`
- manual trigger from the Actions tab

## Local Run

You can also run the sync locally with Node.js 20+:

```bash
node scripts/sync-supabase-storage.mjs
```
