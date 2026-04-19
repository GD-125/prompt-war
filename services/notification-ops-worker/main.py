"""
VenueIQ Notification + Ops Metrics Worker - Cloud Run
Sends push alerts to users, aggregates metrics for ops dashboard.
PORT: 8080 | Stateless
"""

import os
import json
import base64
import logging
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore
import httpx
import uvicorn

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "venueiq-prod")
FCM_SERVER_KEY = os.environ.get("FCM_SERVER_KEY", "")  # Firebase Cloud Messaging
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("notification-ops-worker")

app = FastAPI(title="Notification + Ops Worker")
_db: Optional[firestore.AsyncClient] = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=PROJECT_ID)
    return _db

# ── Notification Templates ────────────────────────────────────────────────────

NOTIFICATION_TEMPLATES = {
    "zone_critical": {
        "title": "⚠️ Area Getting Crowded",
        "body": "Your current area is getting crowded. Consider moving to {alternative_zone}.",
        "priority": "high",
    },
    "queue_alert": {
        "title": "⏱ Wait Time Update",
        "body": "{service_point} wait is now {wait_minutes} min. Try {alternative} instead.",
        "priority": "normal",
    },
    "gate_open": {
        "title": "🚪 New Gate Available",
        "body": "Gate {gate_name} is now open with minimal wait.",
        "priority": "normal",
    },
    "halftime_rush": {
        "title": "🏟 Halftime Head Start",
        "body": "Halftime in 5 min. Head to {recommended_zone} now to beat the rush.",
        "priority": "high",
    },
    "parking_alert": {
        "title": "🚗 Parking Tip",
        "body": "Exit via {gate_name} for fastest parking access after the event.",
        "priority": "low",
    },
}

async def send_fcm_notification(fcm_token: str, title: str, body: str, data: Dict = None):
    """Send push notification via Firebase Cloud Messaging."""
    if not FCM_SERVER_KEY:
        logger.warning("FCM_SERVER_KEY not configured — notification skipped")
        return False

    payload = {
        "to": fcm_token,
        "notification": {"title": title, "body": body},
        "data": data or {},
        "priority": "high",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://fcm.googleapis.com/fcm/send",
            json=payload,
            headers={
                "Authorization": f"key={FCM_SERVER_KEY}",
                "Content-Type": "application/json",
            },
            timeout=5.0,
        )
        if response.status_code == 200:
            logger.info(f"FCM notification sent to {fcm_token[:12]}...")
            return True
        else:
            logger.error(f"FCM error: {response.status_code} {response.text}")
            return False

async def store_notification(venue_id: str, user_id: str, notification: Dict):
    """Store notification in Firestore for in-app display."""
    db = get_db()
    notif_id = f"{venue_id}_{user_id}_{datetime.now(timezone.utc).timestamp()}"
    await db.collection("notifications").document(notif_id).set({
        "venue_id": venue_id,
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read": False,
        **notification,
    })

# ── Ops Metrics Aggregation ───────────────────────────────────────────────────

async def aggregate_ops_metrics(venue_id: str):
    """
    Aggregate real-time metrics for ops dashboard.
    Combines density + queue + session data.
    """
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Fetch all relevant documents in parallel
    density_doc, queue_doc = await db.transaction().get([
        db.collection("venue_density").document(venue_id),
        db.collection("queue_estimates").document(venue_id),
    ])

    density_data = density_doc.to_dict() if density_doc.exists else {}
    queue_data = queue_doc.to_dict() if queue_doc.exists else {}

    zones = density_data.get("zones", {})
    queues = queue_data.get("queues", [])

    # Aggregate stats
    total_occupancy = sum(z.get("count", 0) for z in zones.values())
    critical_zones = [z_id for z_id, z in zones.items() if z.get("density_level") == "critical"]
    high_zones = [z_id for z_id, z in zones.items() if z.get("density_level") == "high"]

    max_queue = max((q.get("wait_minutes", 0) for q in queues), default=0)
    avg_queue = (
        sum(q.get("wait_minutes", 0) for q in queues) / len(queues) if queues else 0
    )

    # Overall venue health score (0–100, 100 = perfect)
    density_penalty = len(critical_zones) * 15 + len(high_zones) * 7
    queue_penalty = min(max_queue * 1.5, 30)
    health_score = max(0, 100 - density_penalty - queue_penalty)

    metrics = {
        "venue_id": venue_id,
        "updated_at": now,
        "summary": {
            "total_occupancy": total_occupancy,
            "health_score": round(health_score, 1),
            "critical_zones": critical_zones,
            "high_density_zones": high_zones,
            "max_queue_minutes": round(max_queue, 1),
            "avg_queue_minutes": round(avg_queue, 1),
            "active_alerts": len(critical_zones) + (1 if max_queue > 15 else 0),
        },
        "zones": zones,
        "queues": queues,
        "recommendations": generate_ops_recommendations(
            critical_zones, high_zones, queues
        ),
    }

    await db.collection("ops_metrics").document(venue_id).set(metrics)
    logger.info(f"Ops metrics updated: venue={venue_id} health={health_score:.0f}")
    return metrics

def generate_ops_recommendations(
    critical_zones: list, high_zones: list, queues: list
) -> list:
    """Generate actionable recommendations for ops staff."""
    recs = []

    if critical_zones:
        recs.append({
            "priority": "urgent",
            "action": f"Deploy crowd management to: {', '.join(critical_zones[:3])}",
            "type": "staffing",
        })

    long_queues = [q for q in queues if q.get("wait_minutes", 0) > 12]
    if long_queues:
        points = [q["point_id"] for q in long_queues[:2]]
        recs.append({
            "priority": "high",
            "action": f"Open additional service lanes at: {', '.join(points)}",
            "type": "service_capacity",
        })

    if "main_concourse_n" in critical_zones and "main_concourse_s" not in critical_zones:
        recs.append({
            "priority": "medium",
            "action": "Redirect north concourse traffic to south — push in-app notification",
            "type": "traffic_management",
        })

    return recs

# ── Event Handlers ────────────────────────────────────────────────────────────

async def handle_notification_trigger(payload: Dict, event_id: str):
    """Process notification events — look up user FCM token and send."""
    venue_id = payload.get("venue_id")
    user_id = payload.get("user_id")
    notification_type = payload.get("notification_type")

    if not all([venue_id, user_id, notification_type]):
        return

    template = NOTIFICATION_TEMPLATES.get(notification_type)
    if not template:
        logger.warning(f"Unknown notification type: {notification_type}")
        return

    # Format message from template
    title = template["title"]
    body = template["body"].format(**payload.get("template_vars", {}))

    # Fetch user FCM token from Firestore
    db = get_db()
    user_doc = await db.collection("user_profiles").document(f"{venue_id}_{user_id}").get()
    if not user_doc.exists:
        # Store for in-app display only
        await store_notification(venue_id, user_id, {"title": title, "body": body})
        return

    fcm_token = user_doc.to_dict().get("fcm_token")
    if fcm_token:
        await send_fcm_notification(fcm_token, title, body, payload.get("data", {}))

    await store_notification(venue_id, user_id, {
        "title": title,
        "body": body,
        "type": notification_type,
    })

async def handle_metrics_update(payload: Dict, event_id: str):
    """Triggered on any density or queue update — refresh ops metrics."""
    venue_id = payload.get("venue_id")
    if venue_id:
        await aggregate_ops_metrics(venue_id)

# ── Pub/Sub Push Endpoint ─────────────────────────────────────────────────────

@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    try:
        body = await request.json()
        message = body.get("message", {})
        data = base64.b64decode(message.get("data", "")).decode("utf-8")
        envelope = json.loads(data)

        event_type = envelope.get("event_type")
        event_id = envelope.get("event_id", "unknown")
        payload = envelope.get("payload", {})

        # Idempotency
        db = get_db()
        idem_key = hashlib.sha256(f"notif_{event_id}".encode()).hexdigest()[:16]
        idem_ref = db.collection("processed_events_notif").document(idem_key)
        if (await idem_ref.get()).exists:
            return JSONResponse({"status": "duplicate"}, status_code=200)

        if event_type == "NOTIFICATION_TRIGGER":
            await handle_notification_trigger(payload, event_id)
        elif event_type in ("USER_LOCATION_UPDATE", "IOT_SENSOR_READING"):
            # Aggregate ops metrics on every data update
            await handle_metrics_update(payload, event_id)

        await idem_ref.set({"processed_at": datetime.now(timezone.utc).isoformat()})
        return JSONResponse({"status": "processed"}, status_code=200)

    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Scheduled Aggregation Endpoint (Cloud Scheduler → Cloud Run) ───────────────
@app.post("/scheduled/aggregate-metrics")
async def scheduled_metrics(request: Request):
    """
    Called by Cloud Scheduler every 30 seconds.
    Full venue metrics refresh for all active venues.
    """
    try:
        # In production: fetch active venue IDs from Firestore
        active_venues = ["venue_001", "venue_002"]
        for venue_id in active_venues:
            await aggregate_ops_metrics(venue_id)
        return JSONResponse({"status": "ok", "venues_processed": len(active_venues)})
    except Exception as e:
        logger.error(f"Scheduled aggregation error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "notification-ops-worker"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
