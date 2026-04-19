/**
 * VenueIQ Event Schema Definitions
 * All events conform to CloudEvents v1.0 envelope wrapped in a VenueIQ envelope.
 * Events flow through Google Cloud Pub/Sub on the "venue-events" topic.
 */

// ── Base Envelope (every message) ─────────────────────────────────────────────
export interface VenueIQEventEnvelope<T = unknown> {
  event_id: string;          // UUID v4 — used for idempotency
  event_type: EventType;     // discriminator
  timestamp: string;         // ISO 8601 UTC
  source: string;            // "api-service" | "iot-gateway" | "scheduler"
  schema_version: "1.0";
  payload: T;
}

// ── Event Types ───────────────────────────────────────────────────────────────
export type EventType =
  | "USER_LOCATION_UPDATE"
  | "IOT_SENSOR_READING"
  | "SERVICE_REQUEST"
  | "ROUTING_REQUEST"
  | "NOTIFICATION_TRIGGER"
  | "ZONE_DENSITY_UPDATED"     // emitted by crowd-analysis-worker
  | "QUEUE_ESTIMATE_UPDATED"   // emitted by queue-routing-worker
  | "HALFTIME_SIGNAL"          // emitted by Cloud Scheduler
  | "ALERT_GENERATED";

// ── Payload Types ─────────────────────────────────────────────────────────────

/** Mobile app pings location every 30 seconds */
export interface UserLocationUpdatePayload {
  user_id: string;           // hashed/anonymized device ID
  venue_id: string;          // e.g., "venue_001"
  zone_id: string;           // e.g., "main_concourse_n"
  lat: number;
  lng: number;
  accuracy_m: number;        // GPS accuracy in meters
  request_id: string;        // client-generated for dedup
}

/** IoT sensor data (infrared counters, camera CV, WiFi probe analytics) */
export interface IoTSensorReadingPayload {
  sensor_id: string;
  venue_id: string;
  zone_id: string;
  sensor_type: "infrared" | "camera_cv" | "wifi_probe" | "pressure_mat";
  count: number;             // people detected
  confidence: number;        // 0.0–1.0
  timestamp: string;
  metadata?: {
    camera_id?: string;
    model_version?: string;  // CV model version
    zone_coverage_pct?: number;
  };
}

/** User requests a service lookup or recommendation */
export interface ServiceRequestPayload {
  user_id: string;
  venue_id: string;
  service_type: "food" | "restroom" | "merchandise" | "entry" | "medical";
  zone_id: string;           // user's current zone
  priority: 0 | 1 | 2;      // 0=normal, 1=assisted, 2=emergency
}

/** On-demand route computation request */
export interface RoutingRequestPayload {
  venue_id: string;
  from_zone: string;
  to_zone: string;
  user_id?: string;          // optional, for personalization
  avoid_zones?: string[];    // user preferences
}

/** Notification dispatch event */
export interface NotificationTriggerPayload {
  venue_id: string;
  user_id?: string;           // null = broadcast to all users in zone
  zone_id?: string;           // target zone for broadcast
  notification_type:
    | "zone_critical"
    | "queue_alert"
    | "gate_open"
    | "halftime_rush"
    | "parking_alert"
    | "weather_alert"
    | "emergency";
  template_vars: Record<string, string>;
  channels: ("push" | "in_app" | "sms")[];
  expires_at?: string;        // don't send after this time
}

/** Emitted by crowd-analysis-worker after updating density */
export interface ZoneDensityUpdatedPayload {
  venue_id: string;
  zone_id: string;
  count: number;
  capacity: number;
  density_score: number;     // 0.0–1.0+
  density_level: "low" | "moderate" | "high" | "critical";
  previous_level?: string;
}

/** Emitted by queue-routing-worker after computing estimates */
export interface QueueEstimateUpdatedPayload {
  venue_id: string;
  point_id: string;
  queue_length: number;
  wait_minutes: number;
  status: "normal" | "busy" | "critical";
}

/** Game state signal from external event management system */
export interface HalftimeSignalPayload {
  venue_id: string;
  event_id: string;
  signal_type: "halftime_start" | "halftime_end" | "game_end" | "delay";
  minutes_until?: number;    // for advance warnings
}

// ── Pub/Sub Message Attributes (for server-side filtering) ────────────────────
export interface PubSubMessageAttributes {
  event_type: EventType;
  venue_id: string;
  zone_id?: string;
  sensor_type?: string;
  priority?: string;
}

// ── Firestore Document Schemas ─────────────────────────────────────────────────

export interface VenueDensityDocument {
  venue_id: string;
  updated_at: string;
  zones: Record<string, ZoneData>;
}

export interface ZoneData {
  zone_id: string;
  count: number;
  capacity: number;
  density_score: number;
  density_level: "low" | "moderate" | "high" | "critical";
  source?: string;
  updated_at: string;
}

export interface QueueEstimatesDocument {
  venue_id: string;
  updated_at: string;
  queues: QueueEntry[];
}

export interface QueueEntry {
  point_id: string;
  zone_id: string;
  queue_length: number;
  wait_minutes: number;
  status: "normal" | "busy" | "critical";
  updated_at: string;
}

export interface RoutingCacheDocument {
  from_zone: string;
  to_zone: string;
  path: string[];
  segments: RouteSegment[];
  estimated_walk_minutes: number;
  congestion_score: number;
  computed_at: string;
}

export interface RouteSegment {
  from: string;
  to: string;
  density_level: string;
}

export interface OpsMetricsDocument {
  venue_id: string;
  updated_at: string;
  summary: {
    total_occupancy: number;
    health_score: number;      // 0–100
    critical_zones: string[];
    high_density_zones: string[];
    max_queue_minutes: number;
    avg_queue_minutes: number;
    active_alerts: number;
  };
  zones: Record<string, ZoneData>;
  queues: QueueEntry[];
  recommendations: OpsRecommendation[];
}

export interface OpsRecommendation {
  priority: "urgent" | "high" | "medium" | "low";
  action: string;
  type: "staffing" | "service_capacity" | "traffic_management" | "communication";
}

export interface UserSessionDocument {
  user_id: string;
  venue_id: string;
  current_zone: string;
  last_seen: string;
  fcm_token?: string;
  preferences?: {
    accessibility?: boolean;
    preferred_language?: string;
  };
}

// ── Example Event Instances ───────────────────────────────────────────────────

export const EXAMPLE_LOCATION_EVENT: VenueIQEventEnvelope<UserLocationUpdatePayload> = {
  event_id: "3f2a1b4c-5d6e-7f8a-9b0c-1d2e3f4a5b6c",
  event_type: "USER_LOCATION_UPDATE",
  timestamp: "2024-12-15T19:45:32.123Z",
  source: "api-service",
  schema_version: "1.0",
  payload: {
    user_id: "usr_anon_a1b2c3d4",
    venue_id: "venue_001",
    zone_id: "main_concourse_n",
    lat: 51.5555,
    lng: -0.1234,
    accuracy_m: 4.2,
    request_id: "req_xyz789",
  },
};

export const EXAMPLE_NOTIFICATION_EVENT: VenueIQEventEnvelope<NotificationTriggerPayload> = {
  event_id: "9a8b7c6d-5e4f-3a2b-1c0d-9e8f7a6b5c4d",
  event_type: "NOTIFICATION_TRIGGER",
  timestamp: "2024-12-15T20:00:01.000Z",
  source: "crowd-analysis-worker",
  schema_version: "1.0",
  payload: {
    venue_id: "venue_001",
    zone_id: "main_concourse_n",
    notification_type: "zone_critical",
    template_vars: {
      alternative_zone: "main_concourse_s",
    },
    channels: ["push", "in_app"],
    expires_at: "2024-12-15T20:10:00.000Z",
  },
};
