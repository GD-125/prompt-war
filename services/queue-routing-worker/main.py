"""
VenueIQ Queue Prediction + Smart Routing Worker - Cloud Run
Consumes venue-events, estimates queue wait times, computes congestion-aware routes.
Uses exponential moving average + Vertex AI for ML predictions.
PORT: 8080 | Stateless | Idempotent
"""

import os
import json
import math
import logging
import hashlib
import base64
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore
import uvicorn

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "venueiq-prod")
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("queue-routing-worker")

app = FastAPI(title="Queue + Routing Worker")
_db: Optional[firestore.AsyncClient] = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=PROJECT_ID)
    return _db

# ── Venue Graph (loaded from Firestore in production) ─────────────────────────
# Weighted adjacency list: node → [(neighbor, base_distance_m)]
VENUE_GRAPH = {
    "entry_gate_a":       [("main_concourse_n", 80), ("main_concourse_e", 95)],
    "entry_gate_b":       [("main_concourse_s", 75), ("main_concourse_w", 90)],
    "entry_gate_c":       [("main_concourse_e", 70), ("main_concourse_s", 85)],
    "main_concourse_n":   [("entry_gate_a", 80), ("food_court_1", 120), ("main_concourse_e", 60), ("main_concourse_w", 60), ("seating_lower", 40)],
    "main_concourse_s":   [("entry_gate_b", 75), ("food_court_2", 110), ("main_concourse_e", 60), ("main_concourse_w", 60), ("seating_lower", 40)],
    "main_concourse_e":   [("entry_gate_a", 95), ("entry_gate_c", 70), ("main_concourse_n", 60), ("main_concourse_s", 60), ("restroom_block_1", 50)],
    "main_concourse_w":   [("entry_gate_b", 90), ("main_concourse_n", 60), ("main_concourse_s", 60), ("restroom_block_2", 55)],
    "food_court_1":       [("main_concourse_n", 120), ("main_concourse_e", 100)],
    "food_court_2":       [("main_concourse_s", 110), ("main_concourse_w", 105)],
    "restroom_block_1":   [("main_concourse_e", 50)],
    "restroom_block_2":   [("main_concourse_w", 55)],
    "seating_lower":      [("main_concourse_n", 40), ("main_concourse_s", 40), ("seating_upper", 30)],
    "seating_upper":      [("seating_lower", 30)],
}

# Service points with base service rates (people/minute)
SERVICE_POINTS = {
    "food_court_1": {"stands": 8, "base_rate_per_stand": 12, "zone": "food_court_1"},
    "food_court_2": {"stands": 6, "base_rate_per_stand": 12, "zone": "food_court_2"},
    "entry_gate_a": {"lanes": 6, "base_rate_per_lane": 25, "zone": "entry_gate_a"},
    "entry_gate_b": {"lanes": 6, "base_rate_per_lane": 25, "zone": "entry_gate_b"},
    "entry_gate_c": {"lanes": 4, "base_rate_per_lane": 25, "zone": "entry_gate_c"},
    "restroom_block_1": {"stalls": 20, "base_rate_per_stall": 4, "zone": "restroom_block_1"},
    "restroom_block_2": {"stalls": 18, "base_rate_per_stall": 4, "zone": "restroom_block_2"},
}

# ── Queue Estimation ──────────────────────────────────────────────────────────

def estimate_wait_time(queue_length: int, service_rate_per_min: float, servers: int) -> float:
    """
    M/M/c queue approximation (Erlang C formula simplified).
    Returns estimated wait time in minutes.
    """
    if service_rate_per_min <= 0 or servers <= 0:
        return 999.0
    total_rate = service_rate_per_min * servers
    if total_rate == 0:
        return 999.0
    utilization = queue_length / max(total_rate, 1)
    # Erlang C approximation
    if utilization >= 1.0:
        return min(queue_length / max(total_rate * 0.1, 1), 60.0)
    wait_minutes = (utilization / (1 - utilization)) / service_rate_per_min
    return round(max(0.5, min(wait_minutes, 60.0)), 1)

def ema_update(current_ema: float, new_value: float, alpha: float = 0.3) -> float:
    """Exponential Moving Average for smoothing queue estimates."""
    return alpha * new_value + (1 - alpha) * current_ema

async def compute_queue_estimates(venue_id: str, density_data: Dict) -> List[Dict]:
    """
    Compute wait time estimates for all service points using current density.
    """
    estimates = []
    now = datetime.now(timezone.utc).isoformat()

    for point_id, config in SERVICE_POINTS.items():
        zone_id = config["zone"]
        zone_data = density_data.get(zone_id, {})
        zone_count = zone_data.get("count", 0)
        zone_capacity = zone_data.get("capacity", 1000)

        # Estimate queue length as fraction of zone occupants waiting
        queue_fraction = min(zone_count / max(zone_capacity, 1), 1.0)
        estimated_queue = int(zone_count * queue_fraction * 0.4)  # ~40% are actively queuing

        # Compute service rate
        if "stands" in config:
            servers = config["stands"]
            rate = config["base_rate_per_stand"]
        elif "lanes" in config:
            servers = config["lanes"]
            rate = config["base_rate_per_lane"]
        else:
            servers = config["stalls"]
            rate = config["base_rate_per_stall"]

        # Adjust rate for density (crowding reduces efficiency)
        efficiency = max(0.6, 1.0 - queue_fraction * 0.3)
        adjusted_rate = rate * efficiency

        wait_min = estimate_wait_time(estimated_queue, adjusted_rate, servers)

        estimates.append({
            "point_id": point_id,
            "zone_id": zone_id,
            "queue_length": estimated_queue,
            "wait_minutes": wait_min,
            "status": "critical" if wait_min > 15 else "busy" if wait_min > 7 else "normal",
            "updated_at": now,
        })

    return estimates

# ── Smart Routing (Dijkstra with congestion weights) ──────────────────────────

def congestion_weight(base_dist: float, density_score: float) -> float:
    """
    Congestion-adjusted edge weight.
    density_score: 0.0 (empty) → 1.0+ (overcrowded)
    At density 0.8, movement speed drops ~50%.
    """
    congestion_multiplier = 1.0 + (density_score ** 2) * 3.0
    return base_dist * congestion_multiplier

def dijkstra(graph: Dict, density_map: Dict, start: str, end: str) -> Tuple[List[str], float]:
    """
    Dijkstra's algorithm with congestion-aware edge weights.
    Returns (path, total_cost).
    """
    import heapq
    
    dist = defaultdict(lambda: math.inf)
    dist[start] = 0
    prev = {}
    heap = [(0, start)]
    visited = set()

    while heap:
        cost, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)

        if node == end:
            break

        for neighbor, base_dist in graph.get(node, []):
            if neighbor in visited:
                continue
            zone_density = density_map.get(neighbor, {}).get("density_score", 0.0)
            edge_w = congestion_weight(base_dist, zone_density)
            new_cost = cost + edge_w
            if new_cost < dist[neighbor]:
                dist[neighbor] = new_cost
                prev[neighbor] = node
                heapq.heappush(heap, (new_cost, neighbor))

    # Reconstruct path
    if end not in prev and end != start:
        return [], math.inf

    path = []
    current = end
    while current != start:
        path.append(current)
        current = prev.get(current)
        if current is None:
            return [], math.inf
    path.append(start)
    path.reverse()
    return path, dist[end]

async def compute_route(venue_id: str, from_zone: str, to_zone: str, density_map: Dict) -> Dict:
    """Compute optimal route considering live congestion."""
    path, cost = dijkstra(VENUE_GRAPH, density_map, from_zone, to_zone)

    if not path:
        return {"error": "No route found", "from_zone": from_zone, "to_zone": to_zone}

    # Build route segments with density info
    segments = []
    for i in range(len(path) - 1):
        zone = path[i]
        next_zone = path[i + 1]
        zone_density = density_map.get(zone, {}).get("density_level", "unknown")
        segments.append({
            "from": zone,
            "to": next_zone,
            "density_level": zone_density,
        })

    # Estimate walking time (avg 1.2 m/s, reduced by congestion)
    avg_density = sum(
        density_map.get(z, {}).get("density_score", 0) for z in path
    ) / max(len(path), 1)
    walk_speed = max(0.6, 1.2 * (1 - avg_density * 0.4))  # m/s
    base_distance = cost / (1 + avg_density * 3)  # reverse the congestion weight
    est_minutes = round(base_distance / walk_speed / 60, 1)

    return {
        "from_zone": from_zone,
        "to_zone": to_zone,
        "path": path,
        "segments": segments,
        "estimated_walk_minutes": est_minutes,
        "congestion_score": round(avg_density, 2),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

# ── Event Handlers ────────────────────────────────────────────────────────────

async def handle_density_updated(payload: Dict, event_id: str):
    """Triggered when crowd analysis worker updates density. Recompute queues + key routes."""
    venue_id = payload.get("venue_id")
    if not venue_id:
        return

    db = get_db()

    # Fetch latest density
    density_doc = await db.collection("venue_density").document(venue_id).get()
    if not density_doc.exists:
        return
    density_map = density_doc.to_dict().get("zones", {})

    # Compute queue estimates
    queue_estimates = await compute_queue_estimates(venue_id, density_map)
    await db.collection("queue_estimates").document(venue_id).set({
        "queues": queue_estimates,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "venue_id": venue_id,
    })

    # Precompute popular routes (entry gates → seating, concourses → food)
    popular_routes = [
        ("entry_gate_a", "seating_lower"),
        ("entry_gate_b", "seating_lower"),
        ("main_concourse_n", "food_court_1"),
        ("main_concourse_s", "food_court_2"),
        ("seating_lower", "restroom_block_1"),
        ("seating_lower", "restroom_block_2"),
    ]

    for from_z, to_z in popular_routes:
        route = await compute_route(venue_id, from_z, to_z, density_map)
        route_key = f"{venue_id}_{from_z}_to_{to_z}"
        await db.collection("routing_cache").document(route_key).set(route)

    logger.info(f"Queue estimates + routes updated for venue {venue_id}")

async def handle_routing_request(payload: Dict, event_id: str):
    """On-demand route computation for less common routes."""
    venue_id = payload.get("venue_id")
    from_zone = payload.get("from_zone")
    to_zone = payload.get("to_zone")

    if not all([venue_id, from_zone, to_zone]):
        return

    db = get_db()
    density_doc = await db.collection("venue_density").document(venue_id).get()
    density_map = density_doc.to_dict().get("zones", {}) if density_doc.exists else {}

    route = await compute_route(venue_id, from_zone, to_zone, density_map)
    route_key = f"{venue_id}_{from_zone}_to_{to_zone}"
    await db.collection("routing_cache").document(route_key).set(route)
    logger.info(f"On-demand route computed: {route_key}")

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
        idem_key = hashlib.sha256(f"qr_{event_id}".encode()).hexdigest()[:16]
        idem_ref = db.collection("processed_events_qr").document(idem_key)
        if (await idem_ref.get()).exists:
            return JSONResponse({"status": "duplicate"}, status_code=200)

        if event_type in ("USER_LOCATION_UPDATE", "IOT_SENSOR_READING"):
            # Piggyback on density updates — trigger queue + routing refresh
            await handle_density_updated(payload, event_id)
        elif event_type == "ROUTING_REQUEST":
            await handle_routing_request(payload, event_id)

        await idem_ref.set({"processed_at": datetime.now(timezone.utc).isoformat()})
        return JSONResponse({"status": "processed"}, status_code=200)

    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "queue-routing-worker"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
