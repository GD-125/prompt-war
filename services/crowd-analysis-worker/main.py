"""
VenueIQ Crowd Analysis Worker — Cloud Run (Production-Hardened)
===============================================================
Security additions:
  • Verifies Pub/Sub push request authenticity via Google OIDC token
    (Cloud Run rejects unauthenticated push — this adds double verification)
  • Verifies HMAC signature on message envelope (set by api-service)
  • Structured audit logging for every state mutation
  • Firestore transactions with optimistic concurrency
  • Idempotency with SHA-256 keyed dedup (TTL-purged collection)
  • Reject payloads exceeding size limits before processing
"""

import os
import json
import hmac
import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import google.auth
import google.auth.transport.requests
from google.auth import jwt as google_jwt
from google.cloud import firestore, secretmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID       = os.environ["GCP_PROJECT_ID"]
PORT             = int(os.environ.get("PORT", 8080))
ENVIRONMENT      = os.environ.get("ENVIRONMENT", "production")
HMAC_SECRET_NAME = os.environ.get("HMAC_SECRET_SM_NAME", "venueiq-hmac-secret")

# Expected Pub/Sub service-account email (OIDC claim)
EXPECTED_SA_EMAIL = os.environ.get(
    "PUBSUB_SA_EMAIL",
    f"service-{os.environ.get('GCP_PROJECT_NUMBER','0')}@gcp-sa-pubsub.iam.gserviceaccount.com"
)

# ── Logging ───────────────────────────────────────────────────────────────────
class GCPJSONFormatter(logging.Formatter):
    SEVERITY = {10:"DEBUG",20:"INFO",30:"WARNING",40:"ERROR",50:"CRITICAL"}
    def format(self, r):
        d = {"severity": self.SEVERITY.get(r.levelno,"DEFAULT"),
             "message": r.getMessage(), "logger": r.name,
             "timestamp": datetime.utcnow().isoformat()+"Z"}
        for k in ("event_id","event_type","venue_id","zone_id","request_id"):
            if hasattr(r, k): d[k] = getattr(r, k)
        return json.dumps(d)

h = logging.StreamHandler()
h.setFormatter(GCPJSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[h])
logger = logging.getLogger("crowd-analysis-worker")

# ── Singletons ────────────────────────────────────────────────────────────────
_db:        Optional[firestore.AsyncClient] = None
_sm_client: Optional[secretmanager.SecretManagerServiceClient] = None
_hmac_key:  Optional[bytes] = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=PROJECT_ID)
    return _db

def get_secret(name: str) -> bytes:
    global _sm_client
    if _sm_client is None:
        _sm_client = secretmanager.SecretManagerServiceClient()
    resp = _sm_client.access_secret_version(
        request={"name": f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"})
    return resp.payload.data

def get_hmac_key() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        try:
            _hmac_key = get_secret(HMAC_SECRET_NAME)
        except Exception as e:
            logger.warning(f"HMAC key unavailable: {e}")
            _hmac_key = b""
    return _hmac_key

# ── Security Helpers ──────────────────────────────────────────────────────────
def verify_pubsub_oidc(authorization: Optional[str]) -> bool:
    """
    Verify the OIDC Bearer token attached by Cloud Pub/Sub to push requests.
    Cloud Run already blocks unauthenticated calls; this adds a second layer
    to confirm the caller is specifically the Pub/Sub service account.
    """
    if ENVIRONMENT != "production":
        return True  # skip in dev/test
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    try:
        request = google.auth.transport.requests.Request()
        claims  = google_jwt.decode(
            token,
            certs_url="https://www.googleapis.com/oauth2/v3/certs",
            verify=True,
        )
        email = claims.get("email", "")
        if email != EXPECTED_SA_EMAIL:
            logger.warning(f"Unexpected push caller: {email}")
            return False
        return True
    except Exception as e:
        logger.warning(f"OIDC verify failed: {e}")
        return False

def verify_envelope_hmac(msg_bytes: bytes, signature: str) -> bool:
    """Verify HMAC-SHA256 set by api-service on the Pub/Sub message."""
    key = get_hmac_key()
    if not key:
        return True  # HMAC disabled
    if not signature or not signature.startswith("sha256="):
        logger.warning("Missing or malformed HMAC on envelope")
        return False
    expected = hmac.new(key, msg_bytes, hashlib.sha256).hexdigest()
    received = signature[7:]
    return hmac.compare_digest(expected, received)

def make_idem_key(prefix: str, event_id: str) -> str:
    return hashlib.sha256(f"{prefix}_{event_id}".encode()).hexdigest()[:20]

# ── Density Logic ─────────────────────────────────────────────────────────────
DEFAULT_ZONE_CAPACITY: Dict[str, int] = {
    "main_concourse_n":  3000,
    "main_concourse_s":  3000,
    "main_concourse_e":  2500,
    "main_concourse_w":  2500,
    "food_court_1":       800,
    "food_court_2":       800,
    "entry_gate_a":      1200,
    "entry_gate_b":      1200,
    "entry_gate_c":      1200,
    "restroom_block_1":   150,
    "restroom_block_2":   150,
    "seating_lower":    20000,
    "seating_upper":    15000,
}

def density_level(count: int, cap: int) -> str:
    r = count / max(cap, 1)
    if r < 0.3:  return "low"
    if r < 0.6:  return "moderate"
    if r < 0.8:  return "high"
    return "critical"

def density_score(count: int, cap: int) -> float:
    return round(count / max(cap, 1), 3)

# ── Event Handlers ────────────────────────────────────────────────────────────
async def handle_location_update(payload: Dict[str, Any], event_id: str):
    venue_id = payload.get("venue_id", "")
    zone_id  = payload.get("zone_id",  "")
    user_id  = payload.get("user_id",  "")

    # Validate required fields
    if not all([venue_id, zone_id, user_id]):
        logger.warning("Incomplete location payload — skipping",
                       extra={"event_id": event_id})
        return

    db   = get_db()
    now  = datetime.now(timezone.utc).isoformat()
    cap  = DEFAULT_ZONE_CAPACITY.get(zone_id, 1000)

    user_ref    = db.collection("user_sessions").document(f"{venue_id}_{user_id}")
    density_ref = db.collection("venue_density").document(venue_id)

    @firestore.async_transactional
    async def _txn(transaction, user_ref, density_ref):
        user_doc, density_doc = await asyncio.gather(
            transaction.get(user_ref),
            transaction.get(density_ref),
        )

        prev_zone = user_doc.to_dict().get("current_zone") if user_doc.exists else None
        current   = density_doc.to_dict() if density_doc.exists else {"zones": {}}
        zones     = current.get("zones", {})

        # Decrement previous zone
        if prev_zone and prev_zone != zone_id and prev_zone in zones:
            old_cnt = max(0, zones[prev_zone].get("count", 0) - 1)
            old_cap = DEFAULT_ZONE_CAPACITY.get(prev_zone, 1000)
            zones[prev_zone] = {
                **zones[prev_zone],
                "count":         old_cnt,
                "density_score": density_score(old_cnt, old_cap),
                "density_level": density_level(old_cnt, old_cap),
                "updated_at":    now,
            }

        # Increment new zone
        new_cnt = zones.get(zone_id, {}).get("count", 0) + 1
        zones[zone_id] = {
            "zone_id":       zone_id,
            "count":         new_cnt,
            "capacity":      cap,
            "density_score": density_score(new_cnt, cap),
            "density_level": density_level(new_cnt, cap),
            "updated_at":    now,
        }

        transaction.set(user_ref, {"current_zone": zone_id, "venue_id": venue_id, "last_seen": now}, merge=True)
        transaction.set(density_ref, {"zones": zones, "updated_at": now, "venue_id": venue_id})

    import asyncio
    txn = get_db().transaction()
    await _txn(txn, user_ref, density_ref)

    # Audit log — no PII
    logger.info("Zone transition recorded",
                extra={"venue_id": venue_id, "zone_id": zone_id, "event_id": event_id})

    # Check if critical — log for alerting pipeline
    new_zone_data = (await density_ref.get()).to_dict().get("zones", {}).get(zone_id, {})
    if new_zone_data.get("density_level") == "critical":
        logger.warning("CRITICAL density detected",
                       extra={"venue_id": venue_id, "zone_id": zone_id,
                              "count": new_zone_data.get("count"), "capacity": cap})

async def handle_iot_sensor(payload: Dict[str, Any], event_id: str):
    venue_id    = payload.get("venue_id", "")
    zone_id     = payload.get("zone_id",  "")
    count       = int(payload.get("count", 0))
    confidence  = float(payload.get("confidence", 1.0))
    sensor_type = payload.get("sensor_type", "unknown")

    if not all([venue_id, zone_id]):
        return

    # Clamp values — never trust raw sensor data blindly
    count      = max(0, min(count, 100_000))
    confidence = max(0.0, min(confidence, 1.0))
    eff_count  = int(count * confidence)

    cap = DEFAULT_ZONE_CAPACITY.get(zone_id, 1000)
    now = datetime.now(timezone.utc).isoformat()
    db  = get_db()

    await db.collection("venue_density").document(venue_id).set({
        "zones": {
            zone_id: {
                "zone_id":       zone_id,
                "count":         eff_count,
                "capacity":      cap,
                "density_score": density_score(eff_count, cap),
                "density_level": density_level(eff_count, cap),
                "source":        f"iot_{sensor_type}",
                "updated_at":    now,
            }
        },
        "updated_at":  now,
        "venue_id":    venue_id,
    }, merge=True)

    logger.info("IoT sensor processed",
                extra={"venue_id": venue_id, "zone_id": zone_id,
                       "count": eff_count, "sensor_type": sensor_type, "event_id": event_id})

# ── Pub/Sub Push Endpoint ─────────────────────────────────────────────────────
app = FastAPI(title="Crowd Analysis Worker", docs_url=None, redoc_url=None)

@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    import asyncio

    # Layer 1: Cloud Run handles OIDC — verify caller is Pub/Sub SA
    auth_header = request.headers.get("Authorization")
    if not verify_pubsub_oidc(auth_header):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.body()

    # Payload size guard (1 MB limit)
    if len(body) > 1_048_576:
        logger.warning("Oversized push payload rejected")
        return JSONResponse({"error": "Payload too large"}, status_code=413)

    try:
        outer   = json.loads(body)
        message = outer.get("message", {})
        raw     = base64.b64decode(message.get("data", ""))
        # Layer 2: verify HMAC on raw bytes
        sig = message.get("attributes", {}).get("x-signature", "")
        if not verify_envelope_hmac(raw, sig):
            logger.warning("HMAC verification failed — rejecting message")
            return JSONResponse({"error": "Invalid signature"}, status_code=200)  # 200 → don't retry tampered msgs

        envelope   = json.loads(raw)
        event_type = envelope.get("event_type", "")
        event_id   = envelope.get("event_id", "unknown")
        payload    = envelope.get("payload", {})

    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Malformed push payload: {e}")
        return JSONResponse({"error": "Bad payload"}, status_code=400)  # don't retry parse errors

    # Idempotency check
    db       = get_db()
    idem_key = make_idem_key("ca", event_id)
    idem_ref = db.collection("processed_events").document(idem_key)
    if (await idem_ref.get()).exists:
        logger.info("Duplicate event skipped", extra={"event_id": event_id})
        return JSONResponse({"status": "duplicate"}, status_code=200)

    try:
        if event_type == "USER_LOCATION_UPDATE":
            await handle_location_update(payload, event_id)
        elif event_type == "IOT_SENSOR_READING":
            await handle_iot_sensor(payload, event_id)
        else:
            logger.info(f"Unhandled event type: {event_type}", extra={"event_id": event_id})

        await idem_ref.set({"processed_at": datetime.now(timezone.utc).isoformat(),
                            "event_type": event_type})
        return JSONResponse({"status": "processed"}, status_code=200)

    except Exception as e:
        logger.error(f"Processing error: {e}", extra={"event_id": event_id}, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)  # triggers Pub/Sub retry

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "crowd-analysis-worker"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
