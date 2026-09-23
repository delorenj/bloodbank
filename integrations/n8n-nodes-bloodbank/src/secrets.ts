import { execFile as execFileCallback } from 'node:child_process';
import { promisify } from 'node:util';

const execFile = promisify(execFileCallback);

/** One hour: a webhook secret changes when a person rotates it, not per request. */
export const SECRET_TTL_MS = 60 * 60 * 1000;
/** A forced re-read (after a signature mismatch) at most this often per reference. */
export const FORCED_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

export type SecretReader = (reference: string) => Promise<string>;

export interface SecretResult {
  value: string;
  /** fresh: read now. cache: within TTL. stale: the read failed and the last
   *  good value was served instead. */
  source: 'fresh' | 'cache' | 'stale';
  error?: string;
}

interface Entry {
  value: string;
  at: number;
}

const cache = new Map<string, Entry>();
const inflight = new Map<string, Promise<string>>();
const forcedAt = new Map<string, number>();

/** `op read`, through whatever `op` is first on PATH.
 *
 * On this host that is the ~/.local/bin wrapper, which carries the service
 * account token (and its own shared cache). This in-process cache sits in front
 * of it so a burst of webhook deliveries costs one read, not one per delivery:
 * reading on every delivery is what let 1Password rate limits drop tickets.
 */
export const opRead: SecretReader = async (reference) => {
  const { stdout } = await execFile('op', ['read', reference], {
    encoding: 'utf8',
    timeout: 5000,
    maxBuffer: 16 * 1024,
  });
  const value = stdout.trim();
  if (!value) throw new Error('1Password returned an empty secret');
  return value;
};

export function clearSecretCache(): void {
  cache.clear();
  inflight.clear();
  forcedAt.clear();
}

/** Read a secret reference with a TTL cache and serve-stale-on-error.
 *
 * Concurrent misses for one reference share a single read. When the read fails
 * and a previous value exists, that value is served (source `stale`) — the
 * alternative is dropping a signed delivery because the vault blinked.
 */
export async function cachedSecret(
  reference: string,
  read: SecretReader = opRead,
  options: { now?: number; ttlMs?: number; force?: boolean } = {},
): Promise<SecretResult> {
  const now = options.now ?? Date.now();
  const ttl = options.ttlMs ?? SECRET_TTL_MS;
  const hit = cache.get(reference);
  if (hit && !options.force && now - hit.at < ttl) return { value: hit.value, source: 'cache' };

  let pending = inflight.get(reference);
  if (!pending) {
    pending = read(reference).finally(() => inflight.delete(reference));
    inflight.set(reference, pending);
  }
  try {
    const value = await pending;
    cache.set(reference, { value, at: now });
    return { value, source: 'fresh' };
  } catch (error) {
    if (hit) return { value: hit.value, source: 'stale', error: (error as Error).message };
    throw error;
  }
}

/** May a signature mismatch trigger a forced re-read of this reference now?
 *
 * A rotated secret shows up as a mismatch against the cached value; re-reading
 * once recovers from that. Rate-limited so a stream of forged or misrouted
 * requests cannot turn back into one vault read per request.
 */
export function mayForceRefresh(reference: string, now = Date.now()): boolean {
  const last = forcedAt.get(reference);
  if (last !== undefined && now - last < FORCED_REFRESH_INTERVAL_MS) return false;
  forcedAt.set(reference, now);
  return true;
}
