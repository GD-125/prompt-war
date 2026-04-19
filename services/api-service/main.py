"""
VenueIQ API Service — Cloud Run (Production-Hardened)
=====================================================
Security additions over v1:
  • HMAC-SHA256 request signature validation (IoT sensors / server-to-server)
  • Google OIDC token verification for internal service calls
  • Strict CORS allowlist (no wildcard in production)
  • Input sanitisation – strip control chars, length-cap all strings
  • Rate limiting per IP via in-process token bucket (back-stop before Cloud Armor)
  • Structured audit logging (Cloud Logging JSON format)
  • Secret Manager integration – no secrets in env vars
  • Request-ID propagation for distributed tracing
  • Security headers middleware (HSTS, CSP, X-Frame-Options …)
  • Pub/Sub message envelope HMAC signing so workers can verify origin
"""

import os
import re
import json
import uuid
import hmac
import time
import hashlib
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict
from threading import Lock

import google.auth
import google.auth.transport.requests
from google.auth import jwt as google_jwt
from google.cloud import pubsub_v1, firestore, secretmanager
from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
import uvicorn

# ── Configuration (from environment — values injected by Cloud Run) ───────────
PROJECT_ID               = os.environ["GCP_PROJECT_ID"]            # required
PUBSUB_TOPIC_EVENTS      = os.environ.get("PUBSUB_TOPIC_EVENTS",      "venue-events")
PUBSUB_TOPIC_NOTIF       = os.environ.get("PUBSUB_TOPIC_NOTIFICATIONS","venue-notifications")
PORT                     = int(os.environ.get("PORT", 8080))
API_VERSION              = "v1"
ENVIRONMENT              = os.environ.get("ENVIRONMENT", "production")

# Allowed CORS origins — comma-separated list injected via Secret Manager / env
_RAW_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _RAW_ORIGINS.split(",") if o.strip()] or ["https://venueiq.example.com"]

# Internal service audience for OIDC verification
INTERNAL_AUDIENCE = os.environ.get("INTERNAL_AUDIENCE", f"https://venueiq-api-{PROJECT_ID}.a.run.app")

# HMAC signing secret name in Secret Manager
HMAC_SECRET_NAME = os.environ.get("HMAC_SECRET_SM_NAME", "venueiq-hmac-secret")

# ── Structured Logging ────────────────────────────────────────────────────────
class GCPJSONFormatter(logging.Formatter):
    """Emits logs in Cloud Logging structured JSON format."""
    SEVERITY_MAP = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity":  self.SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "message":   record.getMessage(),
            "logger":    record.name,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        # Attach any extra fields added via logger.info("msg", extra={...})
        for key in ("request_id", "venue_id", "user_id", "event_type", "latency_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)

handler = logging.StreamHandler()
handler.setFormatter(GCPJSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("api-service")

# ── Lazy Singletons ───────────────────────────────────────────────────────────
_publisher: Optional[pubsub_v1.PublisherClient] = None
_db:        Optional[firestore.AsyncClient]     = None
_sm_client: Optional[secretmanager.SecretManagerServiceClient] = None
_hmac_key:  Optional[bytes]                     = None

def get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher

def get_db() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=PROJECT_ID)
    return _db

def get_secret(secret_name: str) -> bytes:
    """Fetch a secret from Secret Manager (cached in process memory)."""
    global _sm_client
    if _sm_client is None:
        _sm_client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
    response = _sm_client.access_secret_version(request={"name": name})
    return response.payload.data

def get_hmac_key() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        try:
            _hmac_key = get_secret(HMAC_SECRET_NAME)
        except Exception as e:
            logger.warning(f"Could not load HMAC key from Secret Manager: {e}. HMAC disabled.")
            _hmac_key = b""
    return _hmac_key

# ── In-Process Rate Limiter (Token Bucket) ────────────────────────────────────
class TokenBucket:
    """Thread-safe token bucket per key (IP / service-account)."""
    def __init__(self, rate: float, capacity: int):
        self.rate     = rate        # tokens refilled per second
        self.capacity = capacity    # max burst
        self._buckets: dict[str, list] = defaultdict(lambda: [capacity, time.monotonic()])
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            tokens, last = self._buckets[key]
            now = time.monotonic()
            # Refill
            tokens = min(self.capacity, tokens + self.rate * (now - last))
            self._buckets[key] = [tokens, now]
            if tokens >= 1:
                self._buckets[key][0] -= 1
                return True
            return False

# 200 req/s burst, 100 req/s sustained per IP
_rate_limiter = TokenBucket(rate=100, capacity=200)

# ── Input Sanitisation ────────────────────────────────────────────────────────
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SAFE_ID_RE      = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

def sanitise_string(value: str, max_len: int = 256) -> str:
    """Strip control characters, normalise unicode, enforce max length."""
    value = unicodedata.normalize("NFC", value)
    value = _CONTROL_CHAR_RE.sub("", value)
    return value[:max_len]

def validate_id(value: str, field: str) -> str:
    """Ensure ID fields contain only safe characters."""
    if not _SAFE_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid characters in {field}")
    return value

# ── HMAC Signature Verification ───────────────────────────────────────────────
def verify_hmac(body: bytes, signature_header: Optional[str]) -> bool:
    """
    Verify X-VenueIQ-Signature header.
    Header format: sha256=<hex_digest>
    Used for IoT sensors and server-to-server calls.
    """
    key = get_hmac_key()
    if not key:
        return True  # HMAC disabled (dev/test)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    received = signature_header[7:]
    return hmac.compare_digest(expected, received)

def sign_payload(payload: bytes) -> str:
    """Sign outgoing Pub/Sub messages so workers can verify origin."""
    key = get_hmac_key()
    if not key:
        return ""
    return "sha256=" + hmac.new(key, payload, hashlib.sha256).hexdigest()

# ── OIDC Token Verification (internal service-to-service) ─────────────────────
def verify_oidc_token(authorization: Optional[str]) -> Optional[dict]:
    """
    Verify Google OIDC Bearer token for internal endpoints.
    Returns decoded claims or None on failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    try:
        request = google.auth.transport.requests.Request()
        claims  = google_jwt.decode(token, certs_url="https://www.googleapis.com/oauth2/v3/certs", verify=True)
        if claims.get("aud") != INTERNAL_AUDIENCE:
            logger.warning("OIDC token audience mismatch", extra={"expected": INTERNAL_AUDIENCE, "got": claims.get("aud")})
            return None
        return claims
    except Exception as e:
        logger.warning(f"OIDC verification failed: {e}")
        return None

# ── Pub/Sub Publisher (with HMAC-signed envelope) ─────────────────────────────
def publish_event(topic_name: str, event_type: str, payload: dict,
                  request_id: str = "", attributes: dict = None) -> str:
    publisher  = get_publisher()
    topic_path = publisher.topic_path(PROJECT_ID, topic_name)

    envelope = {
        "event_id":      str(uuid.uuid4()),
        "event_type":    event_type,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "source":        "api-service",
        "request_id":    request_id,
        "payload":       payload,
    }
    msg_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    signature = sign_payload(msg_bytes)

    attrs = {
        "event_type":  event_type,
        "x-signature": signature,
        **(attributes or {}),
    }

    future = publisher.publish(topic_path, msg_bytes, **attrs)
    msg_id = future.result(timeout=5.0)
    logger.info("Published event", extra={"event_type": event_type, "request_id": request_id})
    return msg_id

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="VenueIQ API",
    description="Real-time venue intelligence — production hardened",
    version="2.0.0",
    docs_url=None if ENVIRONMENT == "production" else "/docs",   # hide docs in prod
    redoc_url=None if ENVIRONMENT == "production" else "/redoc",
    openapi_url=None if ENVIRONMENT == "production" else "/openapi.json",
)

# Strict CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-ID", "X-VenueIQ-Signature", "Authorization"],
    max_age=600,
)

# ── Security Headers Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["Cache-Control"]             = "no-store"
    response.headers["Content-Security-Policy"]   = "default-src 'none'"
    # Remove server fingerprint
    if "Server" in response.headers:
        del response.headers["Server"]
    return response

# ── Rate-Limit + Tracing Middleware ──────────────────────────────────────────
@app.middleware("http")
async def rate_limit_and_trace(request: Request, call_next):
    start = time.monotonic()

    # Propagate or generate request ID
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    # Rate limit by IP (skip health checks)
    if request.url.path not in ("/health", "/ready"):
        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
        if not _rate_limiter.allow(client_ip):
            logger.warning("Rate limit exceeded", extra={"request_id": request_id})
            return JSONResponse({"error": "Too many requests"}, status_code=429,
                                headers={"Retry-After": "1", "X-Request-ID": request_id})

    response = await call_next(request)
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        f"{request.method} {request.url.path} → {response.status_code}",
        extra={"request_id": request_id, "latency_ms": latency_ms},
    )
    return response

# ── Pydantic Models (strict validation) ───────────────────────────────────────
class LocationPing(BaseModel):
    user_id:    str   = Field(..., min_length=1,  max_length=64)
    venue_id:   str   = Field(..., min_length=1,  max_length=32)
    zone_id:    str   = Field(..., min_length=1,  max_length=32)
    lat:        float = Field(..., ge=-90,  le=90)
    lng:        float = Field(..., ge=-180, le=180)
    accuracy_m: float = Field(default=5.0, ge=0, le=500)

    @validator("user_id", "venue_id", "zone_id", pre=True)
    def sanitise_ids(cls, v):
        v = sanitise_string(str(v), 64)
        validate_id(v, "id field")
        return v

class IoTSensorReading(BaseModel):
    sensor_id:   str   = Field(..., min_length=1, max_length=64)
    venue_id:    str   = Field(..., min_length=1, max_length=32)
    zone_id:     str   = Field(..., min_length=1, max_length=32)
    sensor_type: str   = Field(..., pattern=r"^(infrared|camera_cv|wifi_probe|pressure_mat)$")
    count:       int   = Field(..., ge=0, le=100_000)
    confidence:  float = Field(..., ge=0, le=1)
    timestamp:   Optional[str] = None

    @validator("sensor_id", "venue_id", "zone_id", pre=True)
    def sanitise_ids(cls, v):
        return sanitise_string(str(v), 64)

    @validator("timestamp", always=True, pre=True)
    def default_ts(cls, v):
        return v or datetime.now(timezone.utc).isoformat()

class ServiceRequest(BaseModel):
    user_id:      str = Field(..., min_length=1, max_length=64)
    venue_id:     str = Field(..., min_length=1, max_length=32)
    service_type: str = Field(..., pattern=r"^(food|restroom|merchandise|entry|medical)$")
    zone_id:      str = Field(..., min_length=1, max_length=32)
    priority:     int = Field(default=0, ge=0, le=2)

    @validator("user_id", "venue_id", "zone_id", pre=True)
    def sanitise_ids(cls, v):
        return sanitise_string(str(v), 64)

# ── Dependency: verified request ID ──────────────────────────────────────────
def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))

# ── Health & Readiness ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-service", "ts": time.time()}

@app.get("/ready")
async def ready():
    try:
        get_publisher()
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

# ── Location Ping ─────────────────────────────────────────────────────────────
@app.post(f"/api/{API_VERSION}/location/ping", status_code=202)
async def location_ping(
    ping: LocationPing,
    request: Request,
    request_id: str = Depends(get_request_id),
):
    payload = ping.dict()
    payload["request_id"] = request_id
    # Anonymise — never log raw user_id
    logger.info("Location ping received", extra={"venue_id": ping.venue_id, "request_id": request_id})
    try:
        msg_id = publish_event(
            PUBSUB_TOPIC_EVENTS, "USER_LOCATION_UPDATE", payload, request_id,
            attributes={"venue_id": ping.venue_id, "zone_id": ping.zone_id},
        )
        return {"status": "accepted", "request_id": request_id, "msg_id": msg_id}
    except Exception as e:
        logger.error(f"Pub/Sub publish failed: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Event publishing unavailable")

# ── IoT Sensor Ingest (requires HMAC signature) ───────────────────────────────
@app.post(f"/api/{API_VERSION}/sensors/reading", status_code=202)
async def iot_sensor_reading(
    request: Request,
    request_id: str = Depends(get_request_id),
):
    body = await request.body()

    # Verify HMAC signature from sensor gateway
    sig = request.headers.get("X-VenueIQ-Signature")
    if not verify_hmac(body, sig):
        logger.warning("Invalid HMAC on sensor reading", extra={"request_id": request_id})
        raise HTTPException(status_code=401, detail="Invalid request signature")

    try:
        data = json.loads(body)
        reading = IoTSensorReading(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid payload: {e}")

    try:
        msg_id = publish_event(
            PUBSUB_TOPIC_EVENTS, "IOT_SENSOR_READING", reading.dict(), request_id,
            attributes={"venue_id": reading.venue_id, "sensor_type": reading.sensor_type},
        )
        return {"status": "accepted", "msg_id": msg_id}
    except Exception as e:
        logger.error(f"Sensor ingest failed: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Ingest unavailable")

# ── Venue State (public read endpoints) ───────────────────────────────────────
@app.get(f"/api/{API_VERSION}/venue/{{venue_id}}/density")
async def get_density_map(venue_id: str, request_id: str = Depends(get_request_id)):
    venue_id = sanitise_string(venue_id, 32)
    validate_id(venue_id, "venue_id")
    db = get_db()
    try:
        doc = await db.collection("venue_density").document(venue_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Venue not found")
        data = doc.to_dict()
        return {"venue_id": venue_id, "density_map": data.get("zones", {}), "updated_at": data.get("updated_at")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Firestore read error: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Data unavailable")

@app.get(f"/api/{API_VERSION}/venue/{{venue_id}}/queues")
async def get_queue_estimates(venue_id: str, request_id: str = Depends(get_request_id)):
    venue_id = sanitise_string(venue_id, 32)
    validate_id(venue_id, "venue_id")
    db = get_db()
    try:
        doc = await db.collection("queue_estimates").document(venue_id).get()
        if not doc.exists:
            return {"venue_id": venue_id, "queues": [], "updated_at": None}
        data = doc.to_dict()
        return {"venue_id": venue_id, "queues": data.get("queues", []), "updated_at": data.get("updated_at")}
    except Exception as e:
        logger.error(f"Queue read: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Data unavailable")

@app.get(f"/api/{API_VERSION}/venue/{{venue_id}}/routing")
async def get_routing(venue_id: str, from_zone: str, to_zone: str,
                      request_id: str = Depends(get_request_id)):
    venue_id  = sanitise_string(venue_id, 32);  validate_id(venue_id,  "venue_id")
    from_zone = sanitise_string(from_zone, 32); validate_id(from_zone, "from_zone")
    to_zone   = sanitise_string(to_zone,  32);  validate_id(to_zone,   "to_zone")
    db = get_db()
    try:
        route_key = f"{from_zone}_to_{to_zone}"
        doc = await db.collection("routing_cache").document(f"{venue_id}_{route_key}").get()
        if not doc.exists:
            publish_event(PUBSUB_TOPIC_EVENTS, "ROUTING_REQUEST",
                          {"venue_id": venue_id, "from_zone": from_zone, "to_zone": to_zone}, request_id)
            return {"status": "computing", "estimated_ready_ms": 800}
        return {"venue_id": venue_id, "route": doc.to_dict()}
    except Exception as e:
        logger.error(f"Routing: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Routing unavailable")

# ── Service Request ───────────────────────────────────────────────────────────
@app.post(f"/api/{API_VERSION}/services/request", status_code=202)
async def request_service(req: ServiceRequest, request_id: str = Depends(get_request_id)):
    try:
        msg_id = publish_event(PUBSUB_TOPIC_EVENTS, "SERVICE_REQUEST", req.dict(), request_id)
        return {"status": "accepted", "msg_id": msg_id}
    except Exception as e:
        logger.error(f"Service request: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Service unavailable")

# ── Ops Metrics (internal — requires OIDC token) ──────────────────────────────
@app.get(f"/api/{API_VERSION}/ops/{{venue_id}}/metrics")
async def get_ops_metrics(
    venue_id: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    request_id: str = Depends(get_request_id),
):
    # Ops endpoints are restricted to verified internal callers
    if ENVIRONMENT == "production":
        claims = verify_oidc_token(authorization)
        if not claims:
            raise HTTPException(status_code=401, detail="Unauthorized")
        logger.info("Ops metrics access", extra={
            "caller": claims.get("email"), "request_id": request_id
        })

    venue_id = sanitise_string(venue_id, 32); validate_id(venue_id, "venue_id")
    db = get_db()
    try:
        doc = await db.collection("ops_metrics").document(venue_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Venue not found")
        return doc.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ops metrics: {e}", extra={"request_id": request_id})
        raise HTTPException(status_code=503, detail="Metrics unavailable")

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1,
                log_level="info", access_log=False)  # access log handled by middleware
