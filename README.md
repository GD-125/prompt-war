# 🏟 VenueIQ — Real-Time Venue Intelligence Platform

<div align="center">

![Cloud Run](https://img.shields.io/badge/Google_Cloud_Run-4285F4?style=for-the-badge&logo=google-cloud&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Firestore](https://img.shields.io/badge/Firestore-FFCA28?style=for-the-badge&logo=firebase&logoColor=black)
![Pub/Sub](https://img.shields.io/badge/Pub%2FSub-4285F4?style=for-the-badge&logo=google-cloud&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**Event-driven, cloud-native crowd intelligence for 50,000+ capacity sporting venues.**  
Reduces congestion, minimises wait times, and provides real-time insights to attendees and operators — all running serverlessly on Google Cloud Run.

[Live Demo](https://venueiq-gateway-qfh7vz2kya-uc.a.run.app/) · [Architecture Docs](#architecture) · [Deployment Guide](#deployment) · [API Reference](#api-reference)

</div>

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Deployment](#deployment)
- [Security](#security)
- [API Reference](#api-reference)
- [Event Schema](#event-schema)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Failure Handling](#failure-handling)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

VenueIQ is a production-grade, event-driven microservices platform deployed on **Google Cloud Run**. It ingests location pings from mobile apps and IoT sensors, processes them asynchronously via **Google Cloud Pub/Sub**, and delivers real-time crowd density maps, queue predictions, and smart routing to 100,000+ concurrent users with sub-2-second latency.

### What It Solves

| Problem | Solution |
|---|---|
| Crowd bottlenecks at gates/concourses | Real-time density maps + proactive push alerts |
| Long food & restroom queues | M/M/c queue prediction + alternative routing |
| Static venue floor plans | Live digital twin with 5/10/15/30-min ML forecasts |
| Halftime rush chaos | Predictive signal detection 5 minutes in advance |
| Operator guesswork | Aggregated ops dashboard with actionable recommendations |

---

## Features

### Core
- **Crowd Density Engine** — Zone-level occupancy computed from mobile pings + IoT sensors using Firestore atomic transactions
- **Smart Routing** — Dijkstra's algorithm with congestion-weighted edges, precomputed for popular routes
- **Queue Prediction** — Erlang-C (M/M/c) queue model with EMA smoothing, refreshed every 30 seconds
- **Push Notifications** — Firebase Cloud Messaging with template-based alerts, broadcast or targeted
- **Ops Dashboard** — Aggregated health scores, critical zone alerts, and auto-generated staff recommendations

### Bonus
- **ML Forecasting** — Holt-Winters ETS model producing 5/10/15/20/30-minute density forecasts per zone; Vertex AI endpoint for production
- **Halftime Signal Detection** — Monitors seating-vs-concourse divergence to predict crowd migration 5 minutes before halftime
- **Digital Twin** — Canvas-rendered real-time 2.5D stadium visualisation with heat-map, routing, and forecast overlay layers
- **Offline-First SDK** — TypeScript client with IndexedDB caching, sync queue for offline pings, WebSocket auto-reconnect

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENTS                              │
│  📱 Mobile App  🌐 Ops Dashboard  📡 IoT Sensors           │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS
                     ┌─────▼──────┐
                     │ Cloud Armor │  ← DDoS + WAF + Rate Limit
                     └─────┬──────┘
                           │
              ┌────────────▼─────────────┐
              │   venueiq-api            │  Cloud Run
              │   FastAPI · 2 CPU · 1Gi  │  min=2 max=100
              │   Port 8080 · Stateless  │  concurrency=1000
              └──┬──────────┬────────────┘
                 │          │
           Firestore    Pub/Sub: venue-events
           (reads)          │
                   ┌────────┴────────┐────────────────────┐
                   │                 │                    │
        ┌──────────▼──────┐ ┌────────▼─────────┐ ┌────────▼───────────┐
        │ crowd-analysis  │ │ queue-routing    │ │ notification-ops   │
        │ worker          │ │ worker           │ │ worker             │
        │ Cloud Run       │ │ Cloud Run        │ │ Cloud Run          │
        │ internal only   │ │ internal only    │ │ internal only      │
        └──────────┬──────┘ └───────┬──────────┘ └───────┬────────────┘
                   │                │                    │
                   └────────────────┴────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │   Cloud Firestore  │
                          │   venue_density    │
                          │   queue_estimates  │
                          │   routing_cache    │
                          │   ops_metrics      │
                          └────────────────────┘
```

### Data Flow

```
Client → POST /api/v1/location/ping
       → API validates + sanitises input
       → HMAC-sign envelope
       → Publish to Pub/Sub (venue-events)
       → Push subscription → Workers (parallel fan-out)
       → Workers process + write Firestore
       → Client GET /api/v1/venue/{id}/density → Firestore read
```

### Services

| Service | Role | Ingress | Min | Max | CPU | Memory |
|---|---|---|---|---|---|---|
| `venueiq-api` | Gateway, validator, publisher | Public | 2 | 100 | 2 | 1Gi |
| `venueiq-crowd-worker` | Density computation | Internal | 1 | 50 | 1 | 512Mi |
| `venueiq-queue-routing-worker` | Queue prediction + routing | Internal | 1 | 50 | 1 | 512Mi |
| `venueiq-notification-ops-worker` | Push alerts + ops metrics | Internal | 1 | 50 | 1 | 512Mi |
| `venueiq-ml-service` | Forecasting + recommendations | Internal | 1 | 20 | 2 | 1Gi |

---

## Project Structure

```
venueiq/
├── services/
│   ├── api-service/
│   │   ├── main.py              # FastAPI app, security middleware, endpoints
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── crowd-analysis-worker/
│   │   ├── main.py              # Pub/Sub push handler, density engine
│   │   └── Dockerfile
│   ├── queue-routing-worker/
│   │   ├── main.py              # M/M/c queue model, Dijkstra routing
│   │   └── Dockerfile
│   ├── notification-ops-worker/
│   │   ├── main.py              # FCM push, ops aggregation
│   │   └── Dockerfile
│   └── ml-prediction-service/
│       ├── main.py              # ETS forecaster, recommendation engine
│       └── Dockerfile
├── ml/
│   └── train_pipeline.py        # Vertex AI training pipeline
├── mobile-sdk/
│   └── venueiq-sdk.ts           # Offline-first TypeScript SDK
├── cloudbuild.yaml              # CI/CD pipeline
├── DEPLOY.sh                    # Step-by-step deployment script
├── event-schema.ts              # TypeScript event type definitions
└── README.md
```

---

## Prerequisites

### Tools Required

```bash
# 1. Google Cloud SDK
# macOS
brew install google-cloud-sdk

# Linux
curl https://sdk.cloud.google.com | bash

# Windows — download installer from:
# https://cloud.google.com/sdk/docs/install-sdk#windows

# 2. Docker Desktop
# Download from https://www.docker.com/products/docker-desktop/

# 3. Verify installations
gcloud --version    # should be >= 450.0.0
docker --version    # should be >= 24.0.0
```

### GCP Requirements

- A GCP project with **billing enabled**
- Owner or Editor role on the project (for initial setup)
- APIs will be enabled by the deploy script

---

## Deployment

### Quick Start (automated)

```bash
# 1. Clone the repository
git clone https://github.com/your-org/venueiq.git
cd venueiq

# 2. Set your project ID
export PROJECT_ID="your-gcp-project-id"

# 3. Edit configuration at the top of DEPLOY.sh
nano DEPLOY.sh   # set PROJECT_ID, REGION

# 4. Make executable and run
chmod +x DEPLOY.sh
./DEPLOY.sh
```

### Step-by-Step Manual Deployment

#### Step 1 — Authenticate

```bash
# Login with your Google account
gcloud auth login

# Set application default credentials (used by SDKs)
gcloud auth application-default login

# Set your project
gcloud config set project YOUR_PROJECT_ID
gcloud config set run/region us-central1
```

#### Step 2 — Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  monitoring.googleapis.com \
  cloudbuild.googleapis.com
```

#### Step 3 — Create Firestore Database

```bash
gcloud firestore databases create \
  --location=us-central1 \
  --type=firestore-native
```

#### Step 4 — Store Secrets

```bash
# Generate HMAC signing key
HMAC_KEY=$(openssl rand -hex 32)
echo -n "$HMAC_KEY" | gcloud secrets create venueiq-hmac-secret --data-file=-

# Store allowed CORS origins
echo -n "https://your-frontend.com" | gcloud secrets create venueiq-allowed-origins --data-file=-

# Store FCM key (get from Firebase Console)
echo -n "YOUR_FCM_KEY" | gcloud secrets create venueiq-fcm-key --data-file=-
```

#### Step 5 — Create IAM Service Accounts

```bash
# Create service accounts
gcloud iam service-accounts create venueiq-api-sa --display-name="VenueIQ API"
gcloud iam service-accounts create venueiq-worker-sa --display-name="VenueIQ Workers"
gcloud iam service-accounts create venueiq-scheduler-sa --display-name="VenueIQ Scheduler"

API_SA="venueiq-api-sa@$PROJECT_ID.iam.gserviceaccount.com"
WORKER_SA="venueiq-worker-sa@$PROJECT_ID.iam.gserviceaccount.com"
SCHED_SA="venueiq-scheduler-sa@$PROJECT_ID.iam.gserviceaccount.com"

# Bind minimum required roles
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$API_SA" --role="roles/pubsub.publisher"
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$API_SA" --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$WORKER_SA" --role="roles/pubsub.subscriber"
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$WORKER_SA" --role="roles/datastore.user"

# Grant secret access
gcloud secrets add-iam-policy-binding venueiq-hmac-secret \
  --member="serviceAccount:$API_SA" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding venueiq-hmac-secret \
  --member="serviceAccount:$WORKER_SA" --role="roles/secretmanager.secretAccessor"
```

#### Step 6 — Build & Push Images

```bash
# Configure Docker for Artifact Registry
gcloud auth configure-docker us-central1-docker.pkg.dev

# Create registry
gcloud artifacts repositories create venueiq \
  --repository-format=docker --location=us-central1

IMAGE_BASE="us-central1-docker.pkg.dev/$PROJECT_ID/venueiq"

# Build and push each service
for svc in api-service crowd-analysis-worker queue-routing-worker \
           notification-ops-worker ml-prediction-service; do
  IMAGE_NAME="venueiq-${svc%-service}"
  docker build --platform linux/amd64 \
    -t "$IMAGE_BASE/$IMAGE_NAME:latest" \
    services/$svc/
  docker push "$IMAGE_BASE/$IMAGE_NAME:latest"
done
```

#### Step 7 — Deploy API Service

```bash
gcloud run deploy venueiq-api \
  --image="$IMAGE_BASE/venueiq-api:latest" \
  --region=us-central1 \
  --service-account="$API_SA" \
  --port=8080 \
  --cpu=2 --memory=1Gi \
  --min-instances=2 --max-instances=100 \
  --concurrency=1000 --timeout=30s \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,ENVIRONMENT=production" \
  --set-secrets="ALLOWED_ORIGINS=venueiq-allowed-origins:latest" \
  --ingress=all \
  --allow-unauthenticated \
  --execution-environment=gen2

API_URL=$(gcloud run services describe venueiq-api \
  --region=us-central1 --format="value(status.url)")
echo "API live at: $API_URL"
```

#### Step 8 — Deploy Worker Services

```bash
for svc in venueiq-crowd-worker venueiq-queue-routing-worker \
           venueiq-notification-ops-worker venueiq-ml-service; do
  gcloud run deploy $svc \
    --image="$IMAGE_BASE/$svc:latest" \
    --region=us-central1 \
    --service-account="$WORKER_SA" \
    --port=8080 \
    --cpu=1 --memory=512Mi \
    --min-instances=1 --max-instances=50 \
    --concurrency=80 --timeout=120s \
    --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,ENVIRONMENT=production" \
    --set-secrets="HMAC_SECRET_SM_NAME=venueiq-hmac-secret:latest" \
    --ingress=internal \
    --no-allow-unauthenticated \
    --execution-environment=gen2
done
```

#### Step 9 — Create Pub/Sub Subscriptions

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
PUBSUB_SA="service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"

# Allow Pub/Sub to create OIDC tokens
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$PUBSUB_SA" \
  --role="roles/iam.serviceAccountTokenCreator"

# Create topics
gcloud pubsub topics create venue-events --message-retention-duration=10m
gcloud pubsub topics create venue-notifications --message-retention-duration=24h
gcloud pubsub topics create venue-events-dead-letter --message-retention-duration=7d

# Create push subscriptions (one per worker)
for worker in crowd-analysis queue-routing notification-ops ml-prediction; do
  SVC_NAME="venueiq-${worker}-worker"
  [ "$worker" = "ml-prediction" ] && SVC_NAME="venueiq-ml-service"
  WORKER_URL=$(gcloud run services describe $SVC_NAME \
    --region=us-central1 --format="value(status.url)")

  gcloud pubsub subscriptions create "${worker}-sub" \
    --topic=venue-events \
    --push-endpoint="$WORKER_URL/pubsub/push" \
    --push-auth-service-account="$PUBSUB_SA" \
    --ack-deadline=60 \
    --min-retry-delay=10s --max-retry-delay=600s \
    --dead-letter-topic=venue-events-dead-letter \
    --max-delivery-attempts=5

  # Allow Pub/Sub to invoke this worker
  gcloud run services add-iam-policy-binding $SVC_NAME \
    --region=us-central1 \
    --member="serviceAccount:$PUBSUB_SA" \
    --role="roles/run.invoker"
done
```

#### Step 10 — Verify Deployment

```bash
# Health check
curl $API_URL/health
# → {"status":"healthy","service":"api-service"}

# Ready check
curl $API_URL/ready
# → {"status":"ready"}

# Test location ping
curl -X POST $API_URL/api/v1/location/ping \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_usr","venue_id":"venue_001","zone_id":"main_concourse_n","lat":51.5,"lng":-0.1}'
# → {"status":"accepted","request_id":"...","msg_id":"..."}
```

---

## Security

### Security Architecture

```
┌─────────────────────────────────────────────────┐
│  Layer 1: Network                               │
│  • Cloud Armor WAF (SQLi, XSS, DDoS)            │
│  • Rate limiting: 100 req/min per IP            │
│  • TLS 1.2+ enforced (Cloud Run default)        │
├─────────────────────────────────────────────────┤
│  Layer 2: Identity & Access                     │
│  • Cloud Run OIDC (workers: internal only)      │
│  • IAM least-privilege service accounts         │
│  • Pub/Sub push OIDC token verification         │
├─────────────────────────────────────────────────┤
│  Layer 3: Data Integrity                        │
│  • HMAC-SHA256 envelope signing (api → workers) │
│  • Workers reject messages with invalid HMAC    │
│  • 200 ACK on tampered messages (no retry loop) │
├─────────────────────────────────────────────────┤
│  Layer 4: Input Validation                      │
│  • Pydantic strict models on all endpoints      │
│  • Control character stripping                  │
│  • Safe-character regex on all ID fields        │
│  • Payload size limits (1MB cap)                │
├─────────────────────────────────────────────────┤
│  Layer 5: Secrets                               │
│  • All keys in Secret Manager (never in env)    │
│  • Rotation supported via Secret Manager        │
│  • Container images never embed secrets         │
├─────────────────────────────────────────────────┤
│  Layer 6: Runtime                               │
│  • Non-root container user (UID 10001)          │
│  • Read-only filesystem in runtime stage        │
│  • Security response headers on every response  │
│  • Docs endpoints disabled in production        │
└─────────────────────────────────────────────────┘
```

### Rotating the HMAC Key

```bash
# Generate new key
NEW_KEY=$(openssl rand -hex 32)

# Add new version (old version still works during rollover)
echo -n "$NEW_KEY" | gcloud secrets versions add venueiq-hmac-secret --data-file=-

# After all instances have picked up the new version (< 60s), disable old version
OLD_VERSION=$(gcloud secrets versions list venueiq-hmac-secret \
  --filter="state=enabled" --format="value(name)" | sort | head -1)
gcloud secrets versions disable $OLD_VERSION --secret=venueiq-hmac-secret
```

### Security Headers Returned

Every API response includes:

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `X-XSS-Protection` | `1; mode=block` |
| `Cache-Control` | `no-store` |
| `Content-Security-Policy` | `default-src 'none'` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |

---

## API Reference

### Base URL
```
https://YOUR_API_URL/api/v1
```

### Endpoints

#### `GET /health`
Returns service health status. No authentication required.

```json
{ "status": "healthy", "service": "api-service", "ts": 1735000000.0 }
```

---

#### `POST /api/v1/location/ping`
Submit a user location update from the mobile app.

**Request Body**
```json
{
  "user_id":    "usr_anon_a1b2c3",
  "venue_id":   "venue_001",
  "zone_id":    "main_concourse_n",
  "lat":        51.5555,
  "lng":        -0.1234,
  "accuracy_m": 4.2
}
```

**Response** `202 Accepted`
```json
{ "status": "accepted", "request_id": "uuid", "msg_id": "pub-sub-msg-id" }
```

---

#### `POST /api/v1/sensors/reading`
Ingest IoT sensor data. **Requires `X-VenueIQ-Signature` HMAC header.**

```bash
BODY='{"sensor_id":"ir_01","venue_id":"venue_001","zone_id":"entry_gate_a","sensor_type":"infrared","count":847,"confidence":0.97}'
SIG="sha256=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$HMAC_KEY" -hex | cut -d' ' -f2)"

curl -X POST $API_URL/api/v1/sensors/reading \
  -H "Content-Type: application/json" \
  -H "X-VenueIQ-Signature: $SIG" \
  -d "$BODY"
```

---

#### `GET /api/v1/venue/{venue_id}/density`
Returns current zone-level crowd density map.

**Response** `200 OK`
```json
{
  "venue_id": "venue_001",
  "density_map": {
    "main_concourse_n": {
      "zone_id": "main_concourse_n",
      "count": 2847,
      "capacity": 3000,
      "density_score": 0.949,
      "density_level": "critical",
      "updated_at": "2024-12-15T19:45:33Z"
    }
  },
  "updated_at": "2024-12-15T19:45:33Z"
}
```

---

#### `GET /api/v1/venue/{venue_id}/queues`
Returns wait time estimates for all service points.

---

#### `GET /api/v1/venue/{venue_id}/routing?from_zone=X&to_zone=Y`
Returns congestion-aware walking route.

---

#### `GET /api/v1/ops/{venue_id}/metrics`
Returns aggregated ops dashboard data. **Requires OIDC Bearer token.**

---

## Event Schema

All messages on Pub/Sub follow this envelope:

```typescript
interface VenueIQEvent<T> {
  event_id:       string;   // UUID v4 — idempotency key
  event_type:     string;   // see types below
  timestamp:      string;   // ISO 8601 UTC
  source:         string;   // "api-service" | "iot-gateway"
  schema_version: "1.0";
  payload:        T;
}
```

### Event Types

| Event Type | Published By | Consumed By |
|---|---|---|
| `USER_LOCATION_UPDATE` | api-service | all workers |
| `IOT_SENSOR_READING` | api-service | crowd-worker, ml-service |
| `SERVICE_REQUEST` | api-service | ml-service (recommendations) |
| `ROUTING_REQUEST` | api-service | queue-routing-worker |
| `NOTIFICATION_TRIGGER` | crowd-worker | notification-ops-worker |
| `ZONE_DENSITY_UPDATED` | crowd-worker | queue-routing-worker |
| `HALFTIME_SIGNAL` | Cloud Scheduler | notification-ops-worker |

---

## Configuration

### Environment Variables

| Variable | Service | Description | Default |
|---|---|---|---|
| `GCP_PROJECT_ID` | All | GCP project ID | **required** |
| `ENVIRONMENT` | All | `production` or `development` | `production` |
| `PORT` | All | HTTP listen port | `8080` |
| `PUBSUB_TOPIC_EVENTS` | API | Events topic name | `venue-events` |
| `PUBSUB_SA_EMAIL` | Workers | Pub/Sub SA email for OIDC verify | auto |

### Secrets (Secret Manager)

| Secret Name | Contents | Used By |
|---|---|---|
| `venueiq-hmac-secret` | 32-byte hex HMAC key | api-service, workers |
| `venueiq-fcm-key` | Firebase Cloud Messaging key | notification-worker |
| `venueiq-allowed-origins` | Comma-separated CORS origins | api-service |

---

## Monitoring

### Cloud Logging

All services emit structured JSON logs compatible with Cloud Logging:

```bash
# Stream API logs
gcloud run services logs read venueiq-api \
  --region=us-central1 --tail=100 --follow

# Filter for errors only
gcloud logging read \
  'resource.type="cloud_run_revision" severity>=ERROR' \
  --project=YOUR_PROJECT_ID \
  --limit=50
```

### Key Metrics

| Metric | Alert Threshold | Action |
|---|---|---|
| API P99 latency | > 2 seconds | Scale up / investigate |
| Dead letter queue depth | > 0 | Investigate processing errors |
| Worker error rate | > 1% | Check logs, trigger rollback |
| Firestore read latency | > 500ms | Check indexes, capacity |
| Critical zones | > 3 | Ops team notification |

### Dashboard

Import the pre-built dashboard JSON into Cloud Monitoring:

```bash
gcloud monitoring dashboards create \
  --config-from-file=monitoring/dashboard.json
```

---

## Failure Handling

### Pub/Sub Retry Strategy

```
Delivery attempt 1 → fail → wait 10s
Delivery attempt 2 → fail → wait 20s
Delivery attempt 3 → fail → wait 40s
Delivery attempt 4 → fail → wait 80s
Delivery attempt 5 → fail → Dead Letter Topic (7-day retention)
```

Workers return:
- `200` — ACK (processed, duplicate, or deliberately rejected tampered message)
- `4xx` — ACK (malformed payload, don't retry)
- `5xx` — NACK (transient error, retry)

### Idempotency

Every worker checks a SHA-256 dedup key in Firestore before processing:

```python
idem_key = hashlib.sha256(f"ca_{event_id}".encode()).hexdigest()[:20]
# If exists: return 200 "duplicate"
# If not: process → write idem_key → return 200 "processed"
```

Idempotency collections use Firestore TTL (auto-purge after 24 hours).

### Rollback

```bash
# List recent revisions
gcloud run revisions list --service=venueiq-api --region=us-central1

# Roll back to previous revision instantly
gcloud run services update-traffic venueiq-api \
  --to-revisions=venueiq-api-PREVIOUS_REVISION=100 \
  --region=us-central1
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes and add tests
4. Run linting: `ruff check services/ --select E,W,F`
5. Commit: `git commit -m "feat: add my feature"`
6. Push: `git push origin feature/my-feature`
7. Open a Pull Request

### Commit Convention

```
feat:     New feature
fix:      Bug fix
security: Security improvement
perf:     Performance improvement
docs:     Documentation update
ci:       CI/CD changes
```

---

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) — Python web framework
- [Google Cloud Run](https://cloud.google.com/run) — Serverless container runtime
- [Google Cloud Pub/Sub](https://cloud.google.com/pubsub) — Event messaging
- [Cloud Firestore](https://cloud.google.com/firestore) — Real-time NoSQL database
- [Vertex AI](https://cloud.google.com/vertex-ai) — ML platform

---

<div align="center">
Made with ☁️ on Google Cloud · <a href="https://github.com/GD-125/prompt-war">github.com/GD-125/prompt-war</a>
</div>
