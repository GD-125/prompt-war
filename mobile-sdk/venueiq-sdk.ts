/**
 * VenueIQ Offline-First Mobile SDK
 * ──────────────────────────────────────────────────────────────────────────────
 * Implements a local-first architecture:
 *   1. All reads served from local cache (IndexedDB)
 *   2. Writes queued locally and synced when online
 *   3. Background sync via Service Worker
 *   4. Optimistic UI updates with conflict resolution
 *   5. WebSocket reconnection with exponential backoff
 *
 * Usage (React Native / PWA):
 *   import { VenueIQClient } from './venueiq-sdk';
 *   const client = new VenueIQClient({ venueId: 'venue_001', userId: 'usr_xxx' });
 *   await client.init();
 *   const density = await client.getDensityMap();   // served from cache
 *   client.onDensityUpdate(map => updateUI(map));   // real-time when online
 */

import { openDB, IDBPDatabase } from 'idb';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface VenueIQConfig {
  venueId:     string;
  userId:      string;
  apiBase:     string;
  wsBase?:     string;
  syncIntervalMs?: number;   // default 30_000
  maxRetries?:     number;   // default 5
}

export interface ZoneData {
  zone_id:       string;
  count:         number;
  capacity:      number;
  density_score: number;
  density_level: 'low' | 'moderate' | 'high' | 'critical';
  updated_at:    string;
}

export interface DensityMap {
  venue_id:   string;
  zones:      Record<string, ZoneData>;
  updated_at: string;
  source:     'cache' | 'network';
  stale?:     boolean;
}

export interface QueueEstimate {
  point_id:     string;
  wait_minutes: number;
  status:       'normal' | 'busy' | 'critical';
  updated_at:   string;
}

export interface RouteResult {
  path:                   string[];
  segments:               { from: string; to: string; density_level: string }[];
  estimated_walk_minutes: number;
  congestion_score:       number;
}

export interface CrowdForecast {
  zone_id:   string;
  horizons:  Record<string, { predicted_count: number; density_level: string; confidence: number }>;
}

export interface PendingPing {
  id:        string;
  zone_id:   string;
  lat:       number;
  lng:       number;
  timestamp: string;
  retries:   number;
}

// ── IndexedDB Schema ──────────────────────────────────────────────────────────

const DB_NAME    = 'venueiq-offline';
const DB_VERSION = 2;

type VenueIQDB = IDBPDatabase<{
  density:       { key: string; value: DensityMap };
  queues:        { key: string; value: { venue_id: string; queues: QueueEstimate[]; updated_at: string } };
  forecasts:     { key: string; value: { venue_id: string; zones: Record<string, CrowdForecast>; computed_at: string } };
  routes:        { key: string; value: RouteResult & { key: string; cached_at: string } };
  pending_pings: { key: string; value: PendingPing };
  user_recs:     { key: string; value: { recs: unknown; generated_at: string } };
}>;

async function openVenueIQDB(): Promise<VenueIQDB> {
  return openDB(DB_NAME, DB_VERSION, {
    upgrade(db) {
      if (!db.objectStoreNames.contains('density'))       db.createObjectStore('density');
      if (!db.objectStoreNames.contains('queues'))        db.createObjectStore('queues');
      if (!db.objectStoreNames.contains('forecasts'))     db.createObjectStore('forecasts');
      if (!db.objectStoreNames.contains('routes'))        db.createObjectStore('routes');
      if (!db.objectStoreNames.contains('pending_pings')) db.createObjectStore('pending_pings');
      if (!db.objectStoreNames.contains('user_recs'))     db.createObjectStore('user_recs');
    },
  });
}

// ── Sync Queue ────────────────────────────────────────────────────────────────

class SyncQueue {
  private db: VenueIQDB;
  private flushing = false;

  constructor(db: VenueIQDB) { this.db = db; }

  async enqueue(ping: Omit<PendingPing, 'retries'>): Promise<void> {
    const entry: PendingPing = { ...ping, retries: 0 };
    await this.db.put('pending_pings', entry, entry.id);
  }

  async flush(apiBase: string, venueId: string, userId: string): Promise<void> {
    if (this.flushing || !navigator.onLine) return;
    this.flushing = true;

    try {
      const keys = await this.db.getAllKeys('pending_pings');
      for (const key of keys) {
        const ping = await this.db.get('pending_pings', key);
        if (!ping) continue;

        try {
          const res = await fetch(`${apiBase}/api/v1/location/ping`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-User-ID': userId },
            body:    JSON.stringify({
              user_id:  userId,
              venue_id: venueId,
              zone_id:  ping.zone_id,
              lat:      ping.lat,
              lng:      ping.lng,
            }),
            signal: AbortSignal.timeout(5000),
          });

          if (res.ok) {
            await this.db.delete('pending_pings', key);
          } else {
            await this.incrementRetry(key, ping);
          }
        } catch {
          await this.incrementRetry(key, ping);
        }
      }
    } finally {
      this.flushing = false;
    }
  }

  private async incrementRetry(key: string, ping: PendingPing): Promise<void> {
    if (ping.retries >= 5) {
      await this.db.delete('pending_pings', key); // discard stale pings
      return;
    }
    await this.db.put('pending_pings', { ...ping, retries: ping.retries + 1 }, key);
  }

  async pendingCount(): Promise<number> {
    return (await this.db.getAllKeys('pending_pings')).length;
  }
}

// ── WebSocket Manager ─────────────────────────────────────────────────────────

type WSHandler = (data: unknown) => void;

class WebSocketManager {
  private ws:         WebSocket | null = null;
  private handlers:   Map<string, WSHandler[]> = new Map();
  private retryCount  = 0;
  private maxRetries  = 10;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private wsBase:    string,
    private venueId:   string,
    private userId:    string,
  ) {}

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    const url = `${this.wsBase}/ws/venue/${this.venueId}?uid=${this.userId}`;
    this.ws    = new WebSocket(url);

    this.ws.onopen = () => {
      console.info('[VenueIQ WS] Connected');
      this.retryCount = 0;
      this.emit('connection', { status: 'connected' });
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.emit(msg.type, msg.payload);
      } catch { /* ignore malformed */ }
    };

    this.ws.onclose = () => {
      console.info('[VenueIQ WS] Disconnected — scheduling reconnect');
      this.emit('connection', { status: 'disconnected' });
      this.scheduleReconnect();
    };

    this.ws.onerror = (err) => {
      console.warn('[VenueIQ WS] Error', err);
    };
  }

  private scheduleReconnect(): void {
    if (this.retryCount >= this.maxRetries) return;
    const delay = Math.min(30_000, 1000 * (2 ** this.retryCount));
    this.retryCount++;
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  on(type: string, handler: WSHandler): () => void {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type)!.push(handler);
    return () => {
      const arr = this.handlers.get(type) || [];
      this.handlers.set(type, arr.filter(h => h !== handler));
    };
  }

  private emit(type: string, data: unknown): void {
    (this.handlers.get(type) || []).forEach(h => h(data));
  }

  disconnect(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.ws?.close();
    this.ws = null;
  }
}

// ── Main Client ───────────────────────────────────────────────────────────────

export class VenueIQClient {
  private db!:     VenueIQDB;
  private queue!:  SyncQueue;
  private ws?:     WebSocketManager;
  private syncTimer?: ReturnType<typeof setInterval>;
  private pingTimer?: ReturnType<typeof setInterval>;

  constructor(private config: VenueIQConfig) {}

  async init(): Promise<void> {
    this.db    = await openVenueIQDB();
    this.queue = new SyncQueue(this.db);

    // WebSocket for real-time updates
    if (this.config.wsBase) {
      this.ws = new WebSocketManager(
        this.config.wsBase,
        this.config.venueId,
        this.config.userId,
      );
      this.ws.connect();

      // Wire WS events → cache updates
      this.ws.on('density_update', async (data) => {
        await this.cacheDensity(data as DensityMap);
      });
      this.ws.on('queue_update', async (data: unknown) => {
        const d = data as { queues: QueueEstimate[]; updated_at: string };
        await this.db.put('queues', { venue_id: this.config.venueId, ...d }, this.config.venueId);
      });
    }

    // Sync pending pings on connectivity changes
    window.addEventListener('online',  () => this.flushQueue());
    window.addEventListener('offline', () => console.info('[VenueIQ] Offline — pings will queue'));

    // Periodic background sync
    const interval = this.config.syncIntervalMs ?? 30_000;
    this.syncTimer  = setInterval(() => this.backgroundSync(), interval);

    // Location ping every 30s
    this.pingTimer = setInterval(() => this.sendLocationPing(), 30_000);

    // Initial fetch if online
    if (navigator.onLine) {
      await this.backgroundSync();
    }

    console.info('[VenueIQ] Client initialized — offline-first mode active');
  }

  // ── Data Access (cache-first) ───────────────────────────────────────────────

  async getDensityMap(): Promise<DensityMap> {
    const cached = await this.db.get('density', this.config.venueId);
    if (cached) {
      const ageMs = Date.now() - new Date(cached.updated_at).getTime();
      return { ...cached, source: 'cache', stale: ageMs > 120_000 };
    }
    // Cache miss — fetch from network
    return this.fetchAndCacheDensity();
  }

  async getQueueEstimates(): Promise<QueueEstimate[]> {
    const cached = await this.db.get('queues', this.config.venueId);
    if (cached) return cached.queues;
    return this.fetchAndCacheQueues();
  }

  async getRoute(fromZone: string, toZone: string): Promise<RouteResult | null> {
    const key    = `${this.config.venueId}_${fromZone}_${toZone}`;
    const cached = await this.db.get('routes', key);
    if (cached) {
      const ageMs = Date.now() - new Date(cached.cached_at).getTime();
      if (ageMs < 60_000) return cached; // route valid for 60s
    }
    if (!navigator.onLine) return cached ?? null;
    return this.fetchRoute(fromZone, toZone);
  }

  async getForecasts(): Promise<Record<string, CrowdForecast>> {
    const cached = await this.db.get('forecasts', this.config.venueId);
    if (cached) return cached.zones;
    if (!navigator.onLine) return {};
    return this.fetchForecasts();
  }

  async getMyRecommendations(currentZone: string): Promise<unknown> {
    // Always try network first for personalisation; fall back to cache
    if (navigator.onLine) {
      try {
        const res = await fetch(
          `${this.config.apiBase}/api/v1/ml/recommendations/${this.config.venueId}/${this.config.userId}?zone=${currentZone}`,
          { signal: AbortSignal.timeout(3000) },
        );
        const data = await res.json();
        await this.db.put('user_recs', { recs: data, generated_at: new Date().toISOString() }, this.config.userId);
        return data;
      } catch { /* fallthrough to cache */ }
    }
    const cached = await this.db.get('user_recs', this.config.userId);
    return cached?.recs ?? null;
  }

  // ── Location Ping (queued when offline) ────────────────────────────────────

  async sendLocationPing(zoneId?: string, lat?: number, lng?: number): Promise<void> {
    const ping: PendingPing = {
      id:        `ping_${Date.now()}_${Math.random().toString(36).slice(2,7)}`,
      zone_id:   zoneId  ?? 'unknown',
      lat:       lat     ?? 0,
      lng:       lng     ?? 0,
      timestamp: new Date().toISOString(),
      retries:   0,
    };

    if (navigator.onLine) {
      try {
        await fetch(`${this.config.apiBase}/api/v1/location/ping`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ user_id: this.config.userId, venue_id: this.config.venueId, ...ping }),
          signal:  AbortSignal.timeout(4000),
        });
        return;
      } catch { /* queue it */ }
    }

    await this.queue.enqueue(ping);
    console.info(`[VenueIQ] Ping queued (offline). Pending: ${await this.queue.pendingCount()}`);
  }

  // ── Callbacks ──────────────────────────────────────────────────────────────

  onDensityUpdate(handler: (map: DensityMap) => void): () => void {
    return this.ws?.on('density_update', handler as WSHandler) ?? (() => {});
  }

  onQueueUpdate(handler: (queues: QueueEstimate[]) => void): () => void {
    return this.ws?.on('queue_update', (d) => handler((d as { queues: QueueEstimate[] }).queues)) ?? (() => {});
  }

  onAlert(handler: (alert: unknown) => void): () => void {
    return this.ws?.on('alert', handler as WSHandler) ?? (() => {});
  }

  // ── Internal Network Calls ─────────────────────────────────────────────────

  private async fetchAndCacheDensity(): Promise<DensityMap> {
    const res  = await fetch(`${this.config.apiBase}/api/v1/venue/${this.config.venueId}/density`);
    const data = await res.json() as DensityMap;
    await this.cacheDensity(data);
    return { ...data, source: 'network' };
  }

  private async cacheDensity(data: DensityMap): Promise<void> {
    await this.db.put('density', { ...data, source: 'cache' }, this.config.venueId);
  }

  private async fetchAndCacheQueues(): Promise<QueueEstimate[]> {
    const res  = await fetch(`${this.config.apiBase}/api/v1/venue/${this.config.venueId}/queues`);
    const data = await res.json();
    await this.db.put('queues', data, this.config.venueId);
    return data.queues ?? [];
  }

  private async fetchRoute(from: string, to: string): Promise<RouteResult | null> {
    const url = `${this.config.apiBase}/api/v1/venue/${this.config.venueId}/routing?from_zone=${from}&to_zone=${to}`;
    const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
    const data = await res.json() as RouteResult;
    const key  = `${this.config.venueId}_${from}_${to}`;
    await this.db.put('routes', { ...data, key, cached_at: new Date().toISOString() }, key);
    return data;
  }

  private async fetchForecasts(): Promise<Record<string, CrowdForecast>> {
    const res  = await fetch(`${this.config.apiBase}/api/v1/ml/forecasts/${this.config.venueId}`);
    const data = await res.json();
    await this.db.put('forecasts', data, this.config.venueId);
    return data.zone_forecasts ?? {};
  }

  private async backgroundSync(): Promise<void> {
    if (!navigator.onLine) return;
    await Promise.allSettled([
      this.fetchAndCacheDensity(),
      this.fetchAndCacheQueues(),
      this.fetchForecasts(),
      this.flushQueue(),
    ]);
  }

  private async flushQueue(): Promise<void> {
    await this.queue.flush(this.config.apiBase, this.config.venueId, this.config.userId);
  }

  destroy(): void {
    if (this.syncTimer) clearInterval(this.syncTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.ws?.disconnect();
  }
}

// ── Service Worker Registration ────────────────────────────────────────────────

export async function registerServiceWorker(): Promise<void> {
  if (!('serviceWorker' in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.register('/venueiq-sw.js');
    console.info('[VenueIQ SW] Registered:', reg.scope);

    // Background sync for pings
    if ('sync' in reg) {
      await (reg as ServiceWorkerRegistration & { sync: { register(tag: string): Promise<void> } })
        .sync.register('venueiq-ping-sync');
    }
  } catch (err) {
    console.warn('[VenueIQ SW] Registration failed:', err);
  }
}
