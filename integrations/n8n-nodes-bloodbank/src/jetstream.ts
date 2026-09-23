import { connect } from '@nats-io/transport-node';

import type { NatsConnectionOptions } from './nats';

type NatsConnection = Awaited<ReturnType<typeof connect>>;

export const EVENTS_STREAM = 'BLOODBANK_EVENTS';
export const COMMANDS_STREAM = 'BLOODBANK_COMMANDS';

export interface StoredMessage {
  seq: number;
  subject: string;
  time?: string;
  envelope: Record<string, unknown>;
}

/** The two JetStream calls replay needs, behind a seam tests can fake. */
export interface DirectGetter {
  (stream: string, request: Record<string, unknown>): Promise<StoredMessage | null>;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** JetStream direct get over a plain core request.
 *
 * `$JS.API.DIRECT.GET.<stream>` reads a stored message without creating any
 * consumer — durable or ephemeral — so replaying for a manual test leaves no
 * trace on the server. The stream must have `allow_direct` (both Bloodbank
 * streams do). A 404 status reply means "no such message" and is not an error.
 */
export function directGetter(connection: NatsConnection, timeoutMs = 2000): DirectGetter {
  return async (stream, request) => {
    const reply = await connection.request(
      `$JS.API.DIRECT.GET.${stream}`,
      JSON.stringify(request),
      { timeout: timeoutMs },
    );
    const headers = reply.headers;
    if (headers && headers.code === 404) return null;
    if (headers && headers.hasError) {
      throw new Error(`JetStream direct get on ${stream} failed: ${headers.code} ${headers.description}`);
    }
    const seq = Number(headers?.get('Nats-Sequence'));
    const subject = headers?.get('Nats-Subject') || '';
    if (!Number.isFinite(seq) || !subject) {
      throw new Error(`JetStream direct get on ${stream} returned no sequence/subject headers`);
    }
    let envelope: Record<string, unknown> | null = null;
    try {
      envelope = record(JSON.parse(Buffer.from(reply.data).toString('utf8')));
    } catch {
      envelope = null;
    }
    return {
      seq,
      subject,
      time: headers?.get('Nats-Time-Stamp') || undefined,
      envelope: envelope ?? {},
    };
  };
}

export interface FindLatestOptions {
  /** Cap on direct-get round trips across the whole search. */
  maxRequests?: number;
  /** Wall-clock budget in ms. */
  budgetMs?: number;
  /** First backwards window, in stream sequence numbers. */
  initialWindow?: number;
  now?: () => number;
}

/** The newest retained message on `subject` that satisfies `matches`.
 *
 * The newest message on the subject is one `last_by_subj` away. When it does
 * not match (an alias filter, a data filter), walk backwards in growing
 * windows, reading forward inside each window with `next_by_subj` so every
 * round trip lands on a message of this subject, and keep the newest match of
 * the first window that has one. Bounded by request count and time: a manual
 * test must answer quickly, and "nothing recent matched" is an answer.
 */
export async function findLatestMatching(
  get: DirectGetter,
  stream: string,
  subject: string,
  matches: (envelope: Record<string, unknown>) => boolean,
  options: FindLatestOptions = {},
): Promise<StoredMessage | null> {
  const maxRequests = options.maxRequests ?? 2000;
  const now = options.now ?? Date.now;
  const deadline = now() + (options.budgetMs ?? 8000);
  let requests = 0;
  const fetch = async (request: Record<string, unknown>): Promise<StoredMessage | null> => {
    requests += 1;
    return get(stream, request);
  };

  const last = await fetch({ last_by_subj: subject });
  if (!last) return null;
  if (matches(last.envelope)) return last;

  let upper = last.seq;
  let window = Math.max(1, options.initialWindow ?? 4096);
  while (requests < maxRequests && now() < deadline) {
    const lower = Math.max(1, upper - window);
    let cursor = lower;
    let best: StoredMessage | null = null;
    while (requests < maxRequests && now() < deadline) {
      const candidate = await fetch({ seq: cursor, next_by_subj: subject });
      if (!candidate || candidate.seq >= upper) break;
      if (matches(candidate.envelope)) best = candidate;
      cursor = candidate.seq + 1;
    }
    if (best) return best;
    if (lower <= 1) return null;
    upper = lower;
    window *= 4;
  }
  return null;
}

/** Open a short-lived connection, run `work` with a direct getter, close. */
export async function withDirectGet<T>(
  options: NatsConnectionOptions,
  work: (get: DirectGetter) => Promise<T>,
  connectNats: typeof connect = connect,
): Promise<T> {
  const host = options.host || 'localhost';
  const servers = host.includes('://') ? host : `nats://${host}:${options.port ?? 4222}`;
  const connection = await connectNats({
    servers,
    name: 'n8n-bloodbank-replay',
    timeout: options.timeoutMs ?? 5000,
  });
  try {
    return await work(directGetter(connection));
  } finally {
    await connection.close();
  }
}
