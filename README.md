# cronappwriteabuhg17

This repository exports Appwrite database data every hour with GitHub Actions and stores the result in versioned JSON files.

## What gets created

- `data/appwrite/latest.json`: the newest full export
- `data/appwrite/history/snapshot-YYYYMMDDTHHMMSSZ.json`: timestamped history snapshots

## Required GitHub Secrets

Add these repository secrets in GitHub:

- `APPWRITE_ENDPOINT`
- `APPWRITE_PROJECT_ID`
- `APPWRITE_DATABASE_ID`
- `APPWRITE_API_KEY`

Use these values from your Appwrite project:

- Endpoint: your Appwrite API endpoint
- Project ID: your Appwrite project ID
- Database ID: the database to export
- API key: an Appwrite server key with permission to read the target database and collections

## Workflow behavior

- Runs automatically every hour via GitHub Actions cron
- Can also be started manually from the Actions tab with `workflow_dispatch`
- Commits backup changes back into the repository

## Important note about schedule time

GitHub Actions cron uses UTC. The current schedule `0 * * * *` means it runs at the start of every UTC hour.

## Local run

You can test locally with PowerShell:

```powershell
$env:APPWRITE_ENDPOINT="https://tor.cloud.appwrite.io/v1"
$env:APPWRITE_PROJECT_ID="your-project-id"
$env:APPWRITE_DATABASE_ID="your-database-id"
$env:APPWRITE_API_KEY="your-api-key"
python scripts/fetch_appwrite_backup.py
```
