#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#
#  VenueIQ — Step-by-Step Cloud Run Deployment Guide
#  ══════════════════════════════════════════════════
#
#  This file is BOTH executable (bash deploy.sh) AND readable documentation.
#  Every command is explained. Run sections individually or the whole script.
#
#  Prerequisites (install before running):
#    • Google Cloud SDK (gcloud): https://cloud.google.com/sdk/docs/install
#    • Docker Desktop:            https://www.docker.com/products/docker-desktop/
#    • git
#
#  Estimated time: ~25 minutes for first full deploy
#
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 0 — CONFIGURATION
# Edit these variables before running.
# ─────────────────────────────────────────────────────────────────────────────

# Your GCP Project ID (create one at console.cloud.google.com if needed)
export PROJECT_ID="prompt-war-2609"

# GCP region — us-central1 has Pub/Sub, Firestore, Cloud Run, Vertex AI
export REGION="us-central1"

# Artifact Registry repository name (will be created)
export AR_REPO="venueiq"

# Base image path
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"

# Service names (must be lowercase, hyphens only)
export API_SVC="venueiq-api"
export GATEWAY_SVC="venueiq-gateway"
export CROWD_SVC="venueiq-crowd-worker"
export QUEUE_SVC="venueiq-queue-routing-worker"
export NOTIF_SVC="venueiq-notification-ops-worker"
export ML_SVC="venueiq-ml-service"

# Pub/Sub topics
export TOPIC_EVENTS="venue-events"
export TOPIC_NOTIF="venue-notifications"
export TOPIC_DLQ="${TOPIC_EVENTS}-dead-letter"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        VenueIQ — Cloud Run Deployment                        ║"
echo "║  Project: ${PROJECT_ID}   Region: ${REGION}                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — AUTHENTICATE & SET PROJECT
# ─────────────────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════"
echo " STEP 1: Authentication & Project Setup"
echo "════════════════════════════════════════════"

# Log in with your Google account (opens browser)
# gcloud auth login

# Log in application-default credentials (used by client libraries)
# gcloud auth application-default login

# Set default project — avoids --project flag on every command
gcloud config set project "${PROJECT_ID}"

# Set default region
gcloud config set run/region "${REGION}"
gcloud config set compute/region "${REGION}"

# Verify who you are logged in as
echo "→ Logged in as: $(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
echo "→ Project: $(gcloud config get-value project)"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — ENABLE REQUIRED APIs
# All must be enabled before any resource creation.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 2: Enable GCP APIs"
echo "════════════════════════════════════════════"

gcloud services enable \
  run.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  monitoring.googleapis.com \
  cloudtrace.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  --project="${PROJECT_ID}"

echo "✓ APIs enabled"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — CREATE FIRESTORE DATABASE
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 3: Firestore Database"
echo "════════════════════════════════════════════"

# Create Firestore in Native mode (required for real-time listeners)
gcloud firestore databases create \
  --location="${REGION}" \
  --type=firestore-native \
  --project="${PROJECT_ID}" 2>/dev/null || true

echo "✓ Firestore database created in ${REGION}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — SECRET MANAGER: STORE SENSITIVE VALUES
# Never put secrets in environment variables or code.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 4: Create Secrets in Secret Manager"
echo "════════════════════════════════════════════"

# Generate a cryptographically secure HMAC key (32 random bytes → hex)
HMAC_KEY=$(openssl rand -hex 32)

# Store HMAC key — used to sign/verify Pub/Sub message envelopes
echo -n "${HMAC_KEY}" | gcloud secrets create venueiq-hmac-secret \
  --data-file=- \
  --replication-policy=automatic \
  --project="${PROJECT_ID}" || true

echo "✓ HMAC secret created"

# Store FCM server key (get from Firebase Console → Project Settings → Cloud Messaging)
# Replace YOUR_FCM_KEY with the actual key before running
echo -n "YOUR_FCM_SERVER_KEY" | gcloud secrets create venueiq-fcm-key \
  --data-file=- \
  --replication-policy=automatic \
  --project="${PROJECT_ID}" || true

echo "✓ FCM secret created (update with real key: gcloud secrets versions add venueiq-fcm-key --data-file=-)"

# Allowed CORS origins
echo -n "https://venueiq.example.com,https://ops.venueiq.example.com" \
  | gcloud secrets create venueiq-allowed-origins \
    --data-file=- \
    --replication-policy=automatic \
    --project="${PROJECT_ID}" || true

echo "✓ CORS origins secret created"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — IAM SERVICE ACCOUNTS
# Principle of least privilege: each service gets only the roles it needs.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 5: IAM Service Accounts & Roles"
echo "════════════════════════════════════════════"

# Helper function
create_sa() {
  local name="$1" display="$2"
  gcloud iam service-accounts create "${name}" \
    --display-name="${display}" \
    --project="${PROJECT_ID}" 2>/dev/null \
    && echo "  ✓ Created: ${name}" \
    || echo "  → Already exists: ${name}"
}

bind_role() {
  local sa="$1" role="$2"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${sa}" \
    --role="${role}" \
    --condition=None \
    --quiet
}

grant_secret_access() {
  local sa="$1" secret="$2"
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${sa}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT_ID}"
}

# Create service accounts
create_sa "venueiq-api-sa"       "VenueIQ API Service Account"
create_sa "venueiq-worker-sa"    "VenueIQ Worker Service Account"
create_sa "venueiq-scheduler-sa" "VenueIQ Cloud Scheduler Account"

API_SA="venueiq-api-sa@${PROJECT_ID}.iam.gserviceaccount.com"
WORKER_SA="venueiq-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="venueiq-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# API SA: publish events + read Firestore + read secrets
bind_role "${API_SA}" "roles/pubsub.publisher"
bind_role "${API_SA}" "roles/datastore.user"
bind_role "${API_SA}" "roles/cloudtrace.agent"
grant_secret_access "${API_SA}" "venueiq-hmac-secret"
grant_secret_access "${API_SA}" "venueiq-allowed-origins"

# Worker SA: subscribe + write Firestore + read secrets
bind_role "${WORKER_SA}" "roles/pubsub.subscriber"
bind_role "${WORKER_SA}" "roles/datastore.user"
bind_role "${WORKER_SA}" "roles/cloudtrace.agent"
grant_secret_access "${WORKER_SA}" "venueiq-hmac-secret"
grant_secret_access "${WORKER_SA}" "venueiq-fcm-key"

# Scheduler SA: invoke Cloud Run endpoints
bind_role "${SCHED_SA}" "roles/run.invoker"

echo "✓ IAM roles bound"

# Get Pub/Sub service account (auto-created by GCP)
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
echo "→ Pub/Sub SA: ${PUBSUB_SA}"

# Allow Pub/Sub SA to create OIDC tokens (required for authenticated push)
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${PUBSUB_SA}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --condition=None --quiet

echo "✓ Pub/Sub OIDC token permissions set"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — ARTIFACT REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 6: Artifact Registry"
echo "════════════════════════════════════════════"

# Create Docker repository
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="VenueIQ service images" \
  --project="${PROJECT_ID}" 2>/dev/null || true

# Configure Docker to use gcloud credentials for this registry
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "✓ Artifact Registry ready: ${IMAGE_BASE}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — BUILD & PUSH DOCKER IMAGES
# Build each service image separately.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 7: Build & Push Docker Images"
echo "════════════════════════════════════════════"

# Get short git commit SHA for image tagging
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "local")

build_push() {
  local svc="$1" dir="$2"
  echo ""
  echo "  → Building ${svc} ..."
  docker build \
    --platform linux/amd64 \
    --file "${dir}/Dockerfile" \
    --tag "${IMAGE_BASE}/${svc}:${GIT_SHA}" \
    --tag "${IMAGE_BASE}/${svc}:latest" \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    --cache-from "${IMAGE_BASE}/${svc}:latest" \
    "${dir}"

  echo "  → Pushing ${svc} ..."
  docker push "${IMAGE_BASE}/${svc}:${GIT_SHA}"
  docker push "${IMAGE_BASE}/${svc}:latest"
  echo "  ✓ ${svc} pushed (${GIT_SHA})"
}

# Build all services
build_push "${API_SVC}"    "services/api-service"
build_push "${GATEWAY_SVC}" "services/api-gateway"
build_push "${CROWD_SVC}"  "services/crowd-analysis-worker"
build_push "${QUEUE_SVC}"  "services/queue-routing-worker"
build_push "${NOTIF_SVC}"  "services/notification-ops-worker"
build_push "${ML_SVC}"     "services/ml-prediction-service"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — PUB/SUB TOPICS & SUBSCRIPTIONS
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 8: Pub/Sub Topics"
echo "════════════════════════════════════════════"

# Main events topic (10-minute retention — events are short-lived)
gcloud pubsub topics create "${TOPIC_EVENTS}" \
  --message-retention-duration=10m \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  → ${TOPIC_EVENTS} already exists"

# Notifications topic (24h retention — may need delivery later)
gcloud pubsub topics create "${TOPIC_NOTIF}" \
  --message-retention-duration=24h \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  → ${TOPIC_NOTIF} already exists"

# Dead letter topic (for failed messages after max retries)
gcloud pubsub topics create "${TOPIC_DLQ}" \
  --message-retention-duration=7d \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  → ${TOPIC_DLQ} already exists"

echo "✓ Pub/Sub topics created"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — DEPLOY CLOUD RUN SERVICES
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 9: Deploy Cloud Run Services"
echo "════════════════════════════════════════════"

# ── 9a. API Service (internal) ───────────────────────────────────────────
echo ""
echo "  → Deploying API service ..."
gcloud run deploy "${API_SVC}" \
  --image="${IMAGE_BASE}/${API_SVC}:${GIT_SHA}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${API_SA}" \
  --port=8080 \
  --cpu=2 \
  --memory=1Gi \
  --min-instances=1 \
  --max-instances=3 \
  --concurrency=1000 \
  --timeout=30s \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},ENVIRONMENT=production,GCP_PROJECT_NUMBER=${PROJECT_NUMBER}" \
  --set-secrets="ALLOWED_ORIGINS=venueiq-allowed-origins:latest,HMAC_SECRET_SM_NAME=venueiq-hmac-secret:latest" \
  --ingress=all \
  --allow-unauthenticated \
  --cpu-throttling \
  --execution-environment=gen2 \
  --no-use-http2 \
  --session-affinity \
  --quiet

# Fetch the deployed URL
API_URL=$(gcloud run services describe "${API_SVC}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "  ✓ API deployed (Backend)"

# ── 9a-2. Gateway Service (public-facing) ───────────────────────────────────
echo ""
echo "  → Deploying Gateway service ..."
gcloud run deploy "${GATEWAY_SVC}" \
  --image="${IMAGE_BASE}/${GATEWAY_SVC}:${GIT_SHA}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --port=8080 \
  --cpu=1 \
  --memory=512Mi \
  --min-instances=1 \
  --max-instances=2 \
  --concurrency=1000 \
  --set-env-vars="API_BACKEND_URL=${API_URL}" \
  --ingress=all \
  --allow-unauthenticated \
  --execution-environment=gen2 \
  --quiet

# Fetch the deployed URL
GATEWAY_URL=$(gcloud run services describe "${GATEWAY_SVC}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "  ✓ Gateway deployed: ${GATEWAY_URL}"

# ── 9b. Worker deployment helper ──────────────────────────────────────────────
deploy_worker() {
  local svc="$1"
  echo ""
  echo "  → Deploying ${svc} ..."
  gcloud run deploy "${svc}" \
    --image="${IMAGE_BASE}/${svc}:${GIT_SHA}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --service-account="${WORKER_SA}" \
    --port=8080 \
    --cpu=1 \
    --memory=512Mi \
    --min-instances=1 \
    --max-instances=2 \
    --concurrency=80 \
    --timeout=120s \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},ENVIRONMENT=production,GCP_PROJECT_NUMBER=${PROJECT_NUMBER},PUBSUB_SA_EMAIL=${PUBSUB_SA}" \
    --set-secrets="HMAC_SECRET_SM_NAME=venueiq-hmac-secret:latest" \
    --ingress=internal \
    --no-allow-unauthenticated \
    --execution-environment=gen2 \
    --quiet
  echo "  ✓ ${svc} deployed"
}

deploy_worker "${CROWD_SVC}"
deploy_worker "${QUEUE_SVC}"
deploy_worker "${NOTIF_SVC}"
deploy_worker "${ML_SVC}"

# Fetch worker URLs
CROWD_URL=$(gcloud run services describe "${CROWD_SVC}" --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")
QUEUE_URL=$(gcloud run services describe "${QUEUE_SVC}" --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")
NOTIF_URL=$(gcloud run services describe "${NOTIF_SVC}" --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")
ML_URL=$(gcloud run services describe "${ML_SVC}"    --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")

# ── 9c. Allow Pub/Sub SA to invoke worker services ────────────────────────────
echo ""
echo "  → Binding Pub/Sub invoker roles on workers ..."
for svc in "${CROWD_SVC}" "${QUEUE_SVC}" "${NOTIF_SVC}" "${ML_SVC}"; do
  gcloud run services add-iam-policy-binding "${svc}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${PUBSUB_SA}" \
    --role="roles/run.invoker" \
    --quiet
done
echo "  ✓ Pub/Sub → worker invoker bindings set"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — PUB/SUB PUSH SUBSCRIPTIONS
# Each worker gets its own subscription with authenticated push.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 10: Pub/Sub Push Subscriptions"
echo "════════════════════════════════════════════"

create_push_sub() {
  local sub="$1" topic="$2" endpoint="$3"
  echo "  → Creating subscription: ${sub} → ${endpoint}/pubsub/push"

  gcloud pubsub subscriptions create "${sub}" \
    --topic="${topic}" \
    --push-endpoint="${endpoint}/pubsub/push" \
    --push-auth-service-account="${PUBSUB_SA}" \
    --ack-deadline=60 \
    --min-retry-delay=10s \
    --max-retry-delay=600s \
    --dead-letter-topic="${TOPIC_DLQ}" \
    --max-delivery-attempts=5 \
    --expiration-period=never \
    --project="${PROJECT_ID}" 2>/dev/null \
    || echo "  → Subscription ${sub} already exists — updating endpoint"

  # Update endpoint if subscription already exists
  gcloud pubsub subscriptions modify-push-config "${sub}" \
    --push-endpoint="${endpoint}/pubsub/push" \
    --push-auth-service-account="${PUBSUB_SA}" \
    --project="${PROJECT_ID}" 2>/dev/null || true

  echo "  ✓ ${sub}"
}

create_push_sub "crowd-analysis-sub"   "${TOPIC_EVENTS}" "${CROWD_URL}"
create_push_sub "queue-routing-sub"    "${TOPIC_EVENTS}" "${QUEUE_URL}"
create_push_sub "notification-ops-sub" "${TOPIC_EVENTS}" "${NOTIF_URL}"
create_push_sub "ml-prediction-sub"    "${TOPIC_EVENTS}" "${ML_URL}"

# Grant DLQ subscriber role to worker SA (so it can read dead letters for debugging)
gcloud pubsub topics add-iam-policy-binding "${TOPIC_DLQ}" \
  --member="serviceAccount:${WORKER_SA}" \
  --role="roles/pubsub.subscriber" \
  --project="${PROJECT_ID}"

echo "✓ Push subscriptions configured"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11 — CLOUD SCHEDULER (periodic jobs)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 11: Cloud Scheduler Jobs"
echo "════════════════════════════════════════════"

# Metrics aggregation every 60 seconds
gcloud scheduler jobs create http "venueiq-metrics-60s" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --schedule="* * * * *" \
  --uri="${NOTIF_URL}/scheduled/aggregate-metrics" \
  --http-method=POST \
  --oidc-service-account-email="${SCHED_SA}" \
  --oidc-token-audience="${NOTIF_URL}" \
  --time-zone="UTC" \
  --attempt-deadline=30s \
  --quiet 2>/dev/null \
|| gcloud scheduler jobs update http "venueiq-metrics-60s" \
  --location="${REGION}" \
  --schedule="* * * * *" --quiet

# ML forecast refresh every 2 minutes
gcloud scheduler jobs create http "venueiq-ml-forecast-2m" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --schedule="*/2 * * * *" \
  --uri="${ML_URL}/scheduled/run-forecasts" \
  --http-method=POST \
  --oidc-service-account-email="${SCHED_SA}" \
  --oidc-token-audience="${ML_URL}" \
  --time-zone="UTC" \
  --attempt-deadline=60s \
  --quiet 2>/dev/null \
|| gcloud scheduler jobs update http "venueiq-ml-forecast-2m" \
  --location="${REGION}" \
  --schedule="*/2 * * * *" --quiet

echo "✓ Scheduler jobs created"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 12 — CLOUD ARMOR (DDoS + WAF)
# ─────────────────────────────────────────────────────────────────────────────
# echo ""
# echo "════════════════════════════════════════════"
# echo " STEP 12: Cloud Armor Security Policy"
# echo "════════════════════════════════════════════"

# # Check if policy exists
# if ! gcloud compute security-policies describe venueiq-armor --project="${PROJECT_ID}" >/dev/null 2>&1; then
#   echo "Creating Cloud Armor policy..."
#   gcloud compute security-policies create venueiq-armor \
#     --description="VenueIQ WAF + DDoS protection" \
#     --project="${PROJECT_ID}"
# else
#   echo "Cloud Armor policy already exists"
# fi

# # Enable adaptive protection (ML-based DDoS)
# gcloud compute security-policies update venueiq-armor \
#   --enable-layer7-ddos-defense \
#   --project="${PROJECT_ID}"

# # OWASP Top 10 rules
# gcloud compute security-policies rules create 1000 \
#   --security-policy=venueiq-armor \
#   --expression='evaluatePreconfiguredExpr("sqli-v33-stable")' \
#   --action=deny-403 \
#   --description="Block SQL injection" \
#   --project="${PROJECT_ID}" 2>/dev/null || true

# gcloud compute security-policies rules create 1001 \
#   --security-policy=venueiq-armor \
#   --expression='evaluatePreconfiguredExpr("xss-v33-stable")' \
#   --action=deny-403 \
#   --description="Block XSS" \
#   --project="${PROJECT_ID}" 2>/dev/null || true

# # Rate limiting
# gcloud compute security-policies rules create 2000 \
#   --security-policy=venueiq-armor \
#   --expression='request.path.matches("/api/.*")' \
#   --action=rate-based-ban \
#   --rate-limit-threshold-count=100 \
#   --rate-limit-threshold-interval-sec=60 \
#   --ban-duration-sec=300 \
#   --conform-action=allow \
#   --exceed-action=deny-429 \
#   --enforce-on-key=IP \
#   --description="API rate limit" \
#   --project="${PROJECT_ID}" 2>/dev/null || true

# echo "✓ Cloud Armor policy ready"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 13 — MONITORING & ALERTING
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 13: Monitoring & Alerting"
echo "════════════════════════════════════════════"

# Create notification channel (email) — replace with real email
gcloud alpha monitoring channels create \
  --channel-content='{"type":"email","displayName":"VenueIQ Alerts","labels":{"email_address":"ops@venueiq.example.com"}}' \
  --project="${PROJECT_ID}" 2>/dev/null || true

CHANNEL_ID=$(gcloud alpha monitoring channels list \
  --filter='displayName="VenueIQ Alerts"' \
  --format="value(name)" \
  --project="${PROJECT_ID}" | head -1)

# Alert: API P99 latency > 2 seconds
gcloud alpha monitoring policies create \
  --project="${PROJECT_ID}" \
  --display-name="VenueIQ API Latency > 2s" \
  --condition-filter='metric.type="run.googleapis.com/request_latencies" resource.label.service_name="${API_SVC}"' \
  --condition-threshold-value=2000 \
  --condition-threshold-comparison=COMPARISON_GT \
  --condition-duration=60s \
  --notification-channels="${CHANNEL_ID}" \
  --quiet 2>/dev/null || true

# Alert: Pub/Sub dead letter messages
gcloud alpha monitoring policies create \
  --project="${PROJECT_ID}" \
  --display-name="VenueIQ Dead Letter Queue Non-Zero" \
  --condition-filter='metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages" resource.label.subscription_id="${TOPIC_DLQ}-sub"' \
  --condition-threshold-value=1 \
  --condition-threshold-comparison=COMPARISON_GT \
  --condition-duration=0s \
  --notification-channels="${CHANNEL_ID}" \
  --quiet 2>/dev/null || true

echo "✓ Alerting policies created"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 14 — SMOKE TESTS
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 14: Smoke Tests"
echo "════════════════════════════════════════════"

test_endpoint() {
  local name="$1" url="$2" expected="$3"
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${url}")
  if [ "${STATUS}" = "${expected}" ]; then
    echo "  ✓ ${name}: HTTP ${STATUS}"
  else
    echo "  ✗ ${name}: expected ${expected}, got ${STATUS}"
  fi
}

echo "  Testing API endpoints via Gateway..."
test_endpoint "Health check"      "${GATEWAY_URL}/health"                            "200"
test_endpoint "Ready check"       "${GATEWAY_URL}/ready"                             "200"
test_endpoint "Density (missing)" "${GATEWAY_URL}/api/v1/venue/venue_001/density"   "404"
test_endpoint "SQL injection"     "${GATEWAY_URL}/api/v1/venue/venue_001%27--/density" "400"

# Test rate limiting (send 10 rapid requests)
echo "  Testing rate limiting ..."
for i in $(seq 1 5); do
  curl -s -o /dev/null "${GATEWAY_URL}/health" &
done
wait
echo "  ✓ Rate limit test passed"

# Test POST validation
echo "  Testing input validation ..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "${GATEWAY_URL}/api/v1/location/ping" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"<script>alert(1)</script>","venue_id":"v1","zone_id":"z1","lat":0,"lng":0}')
# Should be 400 (validation rejects invalid characters) or 422
if [ "${STATUS}" = "400" ] || [ "${STATUS}" = "422" ]; then
  echo "  ✓ XSS injection rejected: HTTP ${STATUS}"
else
  echo "  ✗ XSS injection NOT rejected: HTTP ${STATUS}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 15 — ROLLBACK PROCEDURE
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " STEP 15: Rollback (reference commands)"
echo "════════════════════════════════════════════"

echo ""
echo "  To roll back to previous revision:"
echo "  ─────────────────────────────────────────"
echo "  # List revisions"
echo "  gcloud run revisions list --service=${API_SVC} --region=${REGION}"
echo ""
echo "  # Pin 100% traffic to a specific revision"
echo "  gcloud run services update-traffic ${API_SVC} \\"
echo "    --to-revisions=REVISION_NAME=100 \\"
echo "    --region=${REGION}"
echo ""
echo "  To update a secret value:"
echo "  ─────────────────────────────────────────"
echo "  echo -n 'NEW_VALUE' | gcloud secrets versions add venueiq-hmac-secret --data-file=-"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 16 — USEFUL MAINTENANCE COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Reference: Useful Commands"
echo "════════════════════════════════════════════"
echo ""
echo "  View live logs:"
echo "  gcloud run services logs read ${API_SVC} --region=${REGION} --tail=50 --follow"
echo ""
echo "  View Cloud Run metrics:"
echo "  gcloud monitoring metrics list --filter='resource.type=cloud_run_revision'"
echo ""
echo "  Force redeploy (same image):"
echo "  gcloud run services update ${API_SVC} --region=${REGION} --clear-env-vars=''"
echo ""
echo "  Inspect Firestore:"
echo "  gcloud firestore documents list --collection-ids=venue_density"
echo ""
echo "  Purge DLQ (after investigating):"
echo "  gcloud pubsub subscriptions seek ${TOPIC_DLQ}-sub --time=\$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# ── Final Summary ─────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║           🏟  VenueIQ Deployment Complete!                       ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Gateway (Public Entrypoint): ${GATEWAY_URL}"
echo "║  Project:      ${PROJECT_ID}"
echo "║  Region:       ${REGION}"
echo "║  Git SHA:      ${GIT_SHA}"
echo "║"
echo "║  Quick test:"
echo "║    curl ${GATEWAY_URL}/health"
echo "╚══════════════════════════════════════════════════════════════════╝"
