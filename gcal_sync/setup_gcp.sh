#!/usr/bin/env bash
# Creates a Google Cloud project, enables the Calendar API, creates a service
# account and downloads its JSON key. Requires an authenticated gcloud.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-exgcal-$(date +%y%m%d)-$RANDOM}"
SA_NAME="${SA_NAME:-gcal-sync}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
KEY_PATH="${KEY_PATH:-$HOME/.config/exchange-gcal-sync/service-account.json}"

gcloud projects create "$PROJECT_ID" --name="Exchange GCal Sync"
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable calendar-json.googleapis.com --project "$PROJECT_ID"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Exchange to Google Calendar sync" --project "$PROJECT_ID"
mkdir -p "$(dirname "$KEY_PATH")"
chmod 700 "$(dirname "$KEY_PATH")"
gcloud iam service-accounts keys create "$KEY_PATH" \
  --iam-account="$SA_EMAIL" --project="$PROJECT_ID"
chmod 600 "$KEY_PATH"

cat <<EOF
project:      $PROJECT_ID
service acct: $SA_EMAIL
key:          $KEY_PATH

Next: share the target Google Calendar with $SA_EMAIL
(Calendar settings -> Share with specific people -> Make changes to events).
EOF
