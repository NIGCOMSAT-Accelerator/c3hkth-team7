/**
 * Redis/Dragonfly-backed cache — the frontend's half of `backend/app/store/cache.py`'s contract.
 *
 * Same rules, same reasons:
 *
 * 1. **Every write carries a TTL.** Eviction is disabled server-wide on this Dragonfly instance
 *    (protects `db0`'s job streams — see the backend's own note), so an untimed key here would be
 *    permanent, not just here-until-evicted. Enforced as a required argument, not a default.
 * 2. **Reads never raise.** A cache is an optimisation; a subscriber searching for their address
 *    must never see an error because Dragonfly is briefly unreachable. Every failure degrades to
 *    a miss.
 * 3. **Keys are namespaced**, distinctly from the backend's own `CACHE_PREFIX`
 *    (`shelter:cache:...`) so the two stay visually separable on one `SCAN` even though they
 *    cannot collide by construction.
 *
 * ## Why this exists at all
 *
 * `api.ts` used to hold this cache as a plain in-memory `Map` — correct for exactly one running
 * UI instance. Scaling to a second replica would split traffic across two caches that never see
 * each other's writes: half of a scaled deployment's searches would never hit what the other half
 * already resolved, and a "no result" masked by one replica's stale process memory could outlive
 * a backend deploy that fixed it for good, on whichever replica happened to serve that request.
 * Sharing the store the backend already runs — same Dragonfly instance, same `db1`, same
 * `CACHE_URL` — removes both failure modes before a second replica ever exists to expose them.
 *
 * Uses `db1` (cache), never `db0` (job streams) — `CACHE_URL` already points there, matching
 * `backend/app/queue/redis_client.get_cache()`.
 */

import "server-only";

import Redis from "ioredis";

const PREFIX = "shelter-ui:cache";

let client: Redis | null | undefined;

function getClient(): Redis | null {
  if (client !== undefined) return client;

  const url = process.env.CACHE_URL;
  if (!url) {
    // No shared cache configured — every call below degrades to a miss. Correct for local
    // development with no Dragonfly running, same posture as a missing `SHELTER_API_KEY`.
    client = null;
    return client;
  }

  client = new Redis(url, {
    // Fail fast rather than queueing behind a dead connection — a hung cache lookup would
    // make an outage slower to notice than no cache at all.
    maxRetriesPerRequest: 1,
    enableOfflineQueue: false,
    connectTimeout: 3000,
    lazyConnect: true,
  });

  client.on("error", (err) => {
    // Never let a connection error surface as an unhandled rejection. Every caller here
    // already treats a miss as normal; this is purely for operator visibility.
    console.error("[shelter] cache connection error:", err.message);
  });

  return client;
}

/** Namespaced key builder, matching `store/cache.key(*parts)`. */
export function key(...parts: string[]): string {
  return [PREFIX, ...parts].join(":");
}

/** Reads never raise — any failure (no client, timeout, bad JSON) degrades to a miss. */
export async function getJSON<T>(cacheKey: string): Promise<T | null> {
  const redis = getClient();
  if (!redis) return null;

  try {
    const raw = await redis.get(cacheKey);
    if (raw === null) return null;
    return JSON.parse(raw) as T;
  } catch (err) {
    console.error(`[shelter] cache read failed for ${cacheKey}:`, err);
    return null;
  }
}

/**
 * Writes never raise either — a failed cache write should not fail the request that produced
 * the value. `ttlSeconds` must be positive; that check throws, since a caller passing 0 or a
 * negative value is a programming error, not a runtime condition to degrade from.
 */
export async function setJSON(
  cacheKey: string,
  value: unknown,
  ttlSeconds: number,
): Promise<void> {
  if (ttlSeconds <= 0) {
    throw new Error(`cache TTL must be positive, got ${ttlSeconds} for key "${cacheKey}"`);
  }

  const redis = getClient();
  if (!redis) return;

  try {
    await redis.set(cacheKey, JSON.stringify(value), "EX", ttlSeconds);
  } catch (err) {
    console.error(`[shelter] cache write failed for ${cacheKey}:`, err);
  }
}
