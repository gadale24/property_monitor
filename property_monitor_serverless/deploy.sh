#!/usr/bin/env bash
# ============================================================
# Property Monitor – Google Cloud Deployment Script
# Run once. After this, everything is automatic.
#
# Prerequisites:
#   gcloud CLI installed & authenticated (gcloud auth login)
#   All variables below filled in
# ============================================================
set -euo pipefail

# ── YOU MUST SET THESE ────────────────────────────────────────
PROJECT_ID="your-gcp-project-id"          # from console.cloud.google.com
EMAIL_USER="gaadaale24@outlook.com"
EMAIL_PASSWORD="your-outlook-app-password" # see setup guide below
EMAIL_TO="gaadaale24@outlook.com"
TARGET_URL="https://rent.placesforpeople.co.uk/properties.aspx?loc=Bristol&lat=51.454513&lon=-2.58791&mil=10&max=9999&bed=1&typ=0&overfifty=2&pag=1"
# ─────────────────────────────────────────────────────────────

REGION="europe-west2"          # London – closest to Bristol
FUNCTION_NAME="property-monitor"
SCHEDULER_JOB="property-monitor-cron"
CHECK_INTERVAL="*/2 * * * *"   # every 2 minutes

echo ""
echo "======================================================"
echo "  Deploying Property Monitor to Google Cloud"
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo "======================================================"
echo ""

# ── Step 1: Set project ───────────────────────────────────────
gcloud config set project "$PROJECT_ID"

# ── Step 2: Enable APIs ───────────────────────────────────────
echo "[1/5] Enabling Cloud APIs..."
gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    firestore.googleapis.com \
    cloudbuild.googleapis.com \
    run.googleapis.com

# ── Step 3: Create Firestore database (if not already exists) ─
echo "[2/5] Ensuring Firestore database exists..."
gcloud firestore databases create \
    --location="eur3" \
    --type=firestore-native \
    2>/dev/null && echo "  Firestore created." || echo "  Firestore already exists – skipping."

# ── Step 4: Deploy Cloud Function ────────────────────────────
echo "[3/5] Deploying Cloud Function..."
gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python311 \
    --entry-point=monitor \
    --trigger-http \
    --allow-unauthenticated \
    --memory=256Mi \
    --timeout=120s \
    --set-env-vars="\
TARGET_URL=${TARGET_URL},\
EMAIL_TO=${EMAIL_TO},\
EMAIL_USER=${EMAIL_USER},\
EMAIL_PASSWORD=${EMAIL_PASSWORD},\
SMTP_HOST=smtp-mail.outlook.com,\
SMTP_PORT=587"

# ── Step 5: Get function URL ──────────────────────────────────
FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --format="value(serviceConfig.uri)")

echo ""
echo "  Function URL: $FUNCTION_URL"

# ── Step 6: Create/update Cloud Scheduler job ─────────────────
echo "[4/5] Setting up Cloud Scheduler ($CHECK_INTERVAL)..."
gcloud scheduler jobs create http "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$CHECK_INTERVAL" \
    --uri="$FUNCTION_URL" \
    --http-method=GET \
    --time-zone="Europe/London" \
    2>/dev/null \
    || \
gcloud scheduler jobs update http "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$CHECK_INTERVAL" \
    --uri="$FUNCTION_URL" \
    --http-method=GET \
    --time-zone="Europe/London"

# ── Step 7: Trigger first run (sets baseline) ─────────────────
echo "[5/5] Running first check to set baseline..."
curl -s "$FUNCTION_URL" && echo ""

echo ""
echo "======================================================"
echo "  DEPLOYMENT COMPLETE"
echo "======================================================"
echo "  Monitor runs every 2 minutes automatically."
echo "  Alerts → $EMAIL_TO"
echo "  Logs   : https://console.cloud.google.com/logs"
echo "  Cost   : \$0/month (free tier)"
echo "======================================================"
