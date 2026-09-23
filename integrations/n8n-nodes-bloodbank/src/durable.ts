import { createHash } from 'node:crypto';

import {
  AckPolicy,
  DeliverPolicy,
  jetstream,
  jetstreamManager,
} from '@nats-io/jetstream';
import type { ConsumerConfig, ConsumerInfo, JetStreamManager, JsMsg } from '@nats-io/jetstream';
import { connect } from '@nats-io/transport-node';

import type { BloodbankSubscription, NatsConnectionOptions } from './nats';

/** Durable JetStream delivery for event triggers.
 *
 * A core-NATS subscription only hears what is published while it is open.
 * n8n re-registers every trigger of a workflow on each save (an API PUT that
 * only changes pinData included) and on every restart, and whatever lands on
 * the bus in that gap was simply lost — a lost `invocation.completed` left an
 * `agent:working` chip stuck on a ticket forever.
 *
 * A durable consumer on BLOODBANK_EVENTS keeps the trigger's position on the
 * server. BLOODBANK_EVENTS uses `limits` retention (7 days), so a consumer does
 * not change what the stream keeps; it only remembers where this trigger got
 * to. Deactivating a workflow stops pulling but keeps the durable, so the next
 * activation resumes where the last one stopped; `inactive_threshold` deletes
 * the durable of a workflow that never comes back.
 *
 * Messages are pulled one at a time and acknowledged after the workflow
 * execution they started has finished (or on emit, if the node says so). One at
 * a time is the point: it keeps a trigger's executions in stream order, so an
 * `invocation.started` is always handled before the `invocation.completed` that
 * follows it, even when both arrive in the same catch-up burst after a restart.
 */

export const DURABLE_DEFAULTS = {
  /** Server-side redelivery timer. Kept alive by `working()` while an execution runs. */
  ackWaitMs: 30_000,
  /** How often an in-flight message tells the server it is still being worked on. */
  workingEveryMs: 10_000,
  /** Poison-message cap: a message that keeps killing the process is dropped after this. */
  maxDeliver: 5,
  /** Upper bound on unacknowledged messages. The loop holds one; the rest is headroom for redeliveries. */
  maxAckPending: 16,
  /** A durable nobody has pulled from for this long is deleted by the server. */
  inactiveThresholdMs: 7 * 24 * 60 * 60 * 1000,
  /** Long-poll length of one pull request. */
  pullExpiresMs: 30_000,
  /** How long `close()` waits for the in-flight message before it lets go. */
  closeGraceMs: 10_000,
};

const NANOS_PER_MS = 1_000_000;
const MAX_NAME_LENGTH = 120;

const nanos = (ms: number): number => Math.round(ms * NANOS_PER_MS);

/** The durable's name: a pure function of (workflow id, node id).
 *
 * Deterministic so a restart, a re-save or a re-activation finds the same
 * consumer. NATS forbids whitespace, `.`, `*`, `>` and path separators in a
 * consumer name, so anything outside `[A-Za-z0-9_-]` becomes `_`. A pair long
 * enough to be unwieldy collapses to a hash, still deterministic.
 */
export function durableConsumerName(workflowId: unknown, nodeId: unknown): string {
  const clean = (value: unknown): string =>
    (typeof value === 'string' || typeof value === 'number' ? String(value) : '')
      .trim()
      .replace(/[^A-Za-z0-9_-]/g, '_');
  const workflow = clean(workflowId);
  const node = clean(nodeId);
  if (!workflow || !node) {
    throw new Error(
      'a durable Bloodbank trigger needs a saved workflow id and a node id; save the workflow first',
    );
  }
  const name = `n8n-${workflow}-${node}`;
  if (name.length <= MAX_NAME_LENGTH) return name;
  const digest = createHash('sha256').update(`${workflow}\0${node}`).digest('hex').slice(0, 32);
  return `n8n-${workflow.slice(0, 40)}-${digest}`;
}

/** Filter fields in the shape the server stores: one subject is `filter_subject`. */
function filterFields(subjects: string[]): Pick<ConsumerConfig, 'filter_subject' | 'filter_subjects'> {
  const sorted = [...new Set(subjects)].sort();
  return sorted.length === 1
    ? { filter_subject: sorted[0], filter_subjects: undefined }
    : { filter_subject: undefined, filter_subjects: sorted };
}

function boundSubjects(config: Partial<ConsumerConfig>): string[] {
  if (Array.isArray(config.filter_subjects) && config.filter_subjects.length) {
    return [...config.filter_subjects].sort();
  }
  return config.filter_subject ? [config.filter_subject] : [];
}

export interface DurableSettings {
  ackWaitMs?: number;
  maxDeliver?: number;
  maxAckPending?: number;
  inactiveThresholdMs?: number;
}

/** The fields this trigger owns on its durable, all of them updatable in place. */
export function durableMutableConfig(
  subjects: string[],
  description: string,
  settings: DurableSettings = {},
): Partial<ConsumerConfig> {
  return {
    description,
    ack_wait: nanos(settings.ackWaitMs ?? DURABLE_DEFAULTS.ackWaitMs),
    max_deliver: settings.maxDeliver ?? DURABLE_DEFAULTS.maxDeliver,
    max_ack_pending: settings.maxAckPending ?? DURABLE_DEFAULTS.maxAckPending,
    inactive_threshold: nanos(settings.inactiveThresholdMs ?? DURABLE_DEFAULTS.inactiveThresholdMs),
    ...filterFields(subjects),
  };
}

/** The config of a durable created for the first time.
 *
 * `deliver_policy: new` — a first activation starts at the tip of the stream.
 * Without it a new durable on a 7-day stream would replay a week of history
 * into the workflow the moment it is switched on.
 */
export function durableCreateConfig(
  name: string,
  subjects: string[],
  description: string,
  settings: DurableSettings = {},
): Partial<ConsumerConfig> {
  return {
    durable_name: name,
    deliver_policy: DeliverPolicy.New,
    ack_policy: AckPolicy.Explicit,
    ...durableMutableConfig(subjects, description, settings),
  };
}

/** Which owned fields differ between the live durable and what this trigger wants. */
export function durableDrift(
  current: Partial<ConsumerConfig>,
  desired: Partial<ConsumerConfig>,
): string[] {
  const drift: string[] = [];
  if (boundSubjects(current).join('\n') !== boundSubjects(desired).join('\n')) drift.push('filter_subjects');
  for (const key of ['description', 'ack_wait', 'max_deliver', 'max_ack_pending', 'inactive_threshold'] as const) {
    if ((current[key] ?? null) !== (desired[key] ?? null)) drift.push(key);
  }
  return drift;
}

/** The slice of the JetStream manager `ensureDurableConsumer` needs; tests fake it. */
export interface ConsumerAdmin {
  info(stream: string, name: string): Promise<ConsumerInfo>;
  add(stream: string, config: Partial<ConsumerConfig>, opts?: { action?: string }): Promise<ConsumerInfo>;
}

export function isConsumerNotFound(error: unknown): boolean {
  const candidate = error as { code?: unknown; api_error?: { err_code?: unknown }; message?: unknown };
  if (candidate?.api_error?.err_code === 10014) return true;
  if (candidate?.code === 10014 || candidate?.code === '10014') return true;
  return /consumer not found/i.test(String(candidate?.message ?? ''));
}

export interface EnsureResult {
  action: 'created' | 'updated' | 'unchanged';
  info: ConsumerInfo;
  drift: string[];
}

/** Create the durable (deliver new) or bring its owned fields up to date.
 *
 * Never deletes and never moves the position: an existing durable keeps where
 * it got to, which is what lets a re-save or a restart resume instead of skip.
 */
export async function ensureDurableConsumer(
  admin: ConsumerAdmin,
  stream: string,
  name: string,
  subjects: string[],
  description: string,
  settings: DurableSettings = {},
): Promise<EnsureResult> {
  const unique = [...new Set(subjects.filter(Boolean))];
  if (!unique.length) throw new Error('a durable trigger needs at least one subject');
  let current: ConsumerInfo | null = null;
  try {
    current = await admin.info(stream, name);
  } catch (error) {
    if (!isConsumerNotFound(error)) throw error;
  }
  if (!current) {
    const info = await admin.add(stream, durableCreateConfig(name, unique, description, settings));
    return { action: 'created', info, drift: [] };
  }
  const config = current.config as Partial<ConsumerConfig>;
  if (config.deliver_subject) {
    throw new Error(
      `consumer ${name} on ${stream} is a push consumer; delete it (nats consumer rm ${stream} ${name}) and re-activate the workflow`,
    );
  }
  if (config.ack_policy && config.ack_policy !== AckPolicy.Explicit) {
    throw new Error(
      `consumer ${name} on ${stream} has ack_policy ${String(config.ack_policy)}; delete it and re-activate the workflow`,
    );
  }
  const desired = durableMutableConfig(unique, description, settings);
  const drift = durableDrift(config, desired);
  if (!drift.length) return { action: 'unchanged', info: current, drift };
  const next: Partial<ConsumerConfig> = { ...config, ...desired };
  if (next.filter_subjects?.length) delete next.filter_subject;
  else delete next.filter_subjects;
  const info = await admin.add(stream, next, { action: 'update' });
  return { action: 'updated', info, drift };
}

/** One pulled message, stripped to what the consume loop needs. */
export interface DurableMessage {
  subject: string;
  data: Uint8Array;
  seq: number;
  /** Stream timestamp in ms since the epoch. */
  timestampMs: number;
  deliveryCount: number;
  ack(): void;
  term(reason?: string): void;
  working(): void;
}

export type DurableVerdict = 'ack' | 'term';

/** The transport the consume loop drives. `jetstreamBackend` is the real one. */
export interface DurableBackend {
  /** Make sure the durable exists and is current; called at start and after pull failures. */
  ensure(): Promise<EnsureResult>;
  /** Long-poll for the next message; null when the poll expired empty. */
  next(expiresMs: number): Promise<DurableMessage | null>;
  /** Push any queued acks to the server. */
  flush(): Promise<void>;
  close(): Promise<void>;
  /** Resolves when the connection closes for good; an Error when it closed on its own. */
  closed(): Promise<Error | void>;
}

export interface DurableConsumeOptions {
  /** Messages older than this (by stream timestamp) are acked and skipped. 0 = no limit. */
  catchUpWindowMs?: number;
  pullExpiresMs?: number;
  workingEveryMs?: number;
  closeGraceMs?: number;
  /** Decide a message. Resolve when it is safe to acknowledge it. */
  onMessage(message: DurableMessage): Promise<DurableVerdict>;
  /** A message the catch-up window dropped. */
  onStale?(message: DurableMessage, ageMs: number): void;
  /** Recoverable trouble (pull failed, consumer recreated). Logged, never fatal. */
  onWarning?(message: string, error?: Error): void;
  /** The connection is gone for good. */
  onFatal(error: Error): void;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    timer.unref?.();
  });

/** Drive a durable: pull one, decide it, acknowledge it, repeat until closed. */
export async function consumeDurable(
  backend: DurableBackend,
  options: DurableConsumeOptions,
): Promise<BloodbankSubscription & { done: Promise<void> }> {
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? defaultSleep;
  const expires = Math.max(1000, options.pullExpiresMs ?? DURABLE_DEFAULTS.pullExpiresMs);
  const workingEvery = options.workingEveryMs ?? DURABLE_DEFAULTS.workingEveryMs;
  const catchUp = Math.max(0, options.catchUpWindowMs ?? 0);
  const warn = (message: string, error?: Error): void => options.onWarning?.(message, error);

  await backend.ensure();

  let closing = false;
  let inflight: Promise<void> = Promise.resolve();

  const handle = async (message: DurableMessage): Promise<void> => {
    const age = now() - message.timestampMs;
    if (catchUp > 0 && Number.isFinite(age) && age > catchUp) {
      message.ack();
      options.onStale?.(message, age);
      return;
    }
    const heartbeat = setInterval(() => {
      try {
        message.working();
      } catch {
        // The ack timer is advisory; a failed heartbeat only risks a redelivery.
      }
    }, workingEvery);
    heartbeat.unref?.();
    try {
      const verdict = await options.onMessage(message);
      if (verdict === 'term') message.term('rejected by n8n Bloodbank trigger');
      else message.ack();
    } catch (error) {
      // A handler that throws would throw again on redelivery: stop it here.
      warn(`message ${message.seq} on ${message.subject} failed; terminated`, error as Error);
      try {
        message.term('n8n Bloodbank trigger handler failed');
      } catch {
        // connection gone; the server redelivers after ack_wait
      }
    } finally {
      clearInterval(heartbeat);
    }
  };

  const loop = async (): Promise<void> => {
    let backoff = 1000;
    while (!closing) {
      let message: DurableMessage | null;
      try {
        message = await backend.next(expires);
        backoff = 1000;
      } catch (error) {
        if (closing) break;
        warn(`pull failed; retrying in ${backoff}ms`, error as Error);
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 30_000);
        if (closing) break;
        try {
          const ensured = await backend.ensure();
          if (ensured.action === 'created') warn('durable consumer was missing and has been recreated');
        } catch (ensureError) {
          warn('durable consumer could not be ensured', ensureError as Error);
        }
        continue;
      }
      if (!message) continue;
      if (closing) break; // unacked: the server redelivers it to the next activation
      inflight = handle(message);
      await inflight;
    }
  };

  const done = loop();
  void backend.closed().then((error) => {
    if (!closing && error) options.onFatal(error);
  });

  return {
    done,
    async close(): Promise<void> {
      if (closing) return;
      closing = true;
      const grace = options.closeGraceMs ?? DURABLE_DEFAULTS.closeGraceMs;
      await Promise.race([inflight, sleep(grace)]);
      try {
        await backend.flush();
      } catch {
        // best effort: an ack that did not make it is a redelivery, not a loss
      }
      await backend.close();
      await Promise.race([done, sleep(grace)]);
    },
  };
}

function toDurableMessage(message: JsMsg): DurableMessage {
  return {
    subject: message.subject,
    data: message.data,
    seq: message.seq,
    timestampMs: Number(message.info.timestampNanos) / NANOS_PER_MS,
    deliveryCount: message.info.deliveryCount,
    ack: () => message.ack(),
    term: (reason?: string) => message.term(reason),
    working: () => message.working(),
  };
}

export interface JetStreamBackendOptions extends NatsConnectionOptions, DurableSettings {
  stream: string;
  name: string;
  subjects: string[];
  description: string;
  connectionName?: string;
  onEnsured?(result: EnsureResult): void;
}

/** The real transport: one reconnecting connection, one durable pull consumer. */
export async function jetstreamBackend(
  options: JetStreamBackendOptions,
  connectNats: typeof connect = connect,
): Promise<DurableBackend> {
  const host = options.host || 'localhost';
  const servers = host.includes('://') ? host : `nats://${host}:${options.port ?? 4222}`;
  const connection = await connectNats({
    servers,
    name: options.connectionName || `n8n-bloodbank-durable:${options.name}`,
    timeout: options.timeoutMs ?? 5000,
    reconnect: true,
    maxReconnectAttempts: -1,
  });
  let manager: JetStreamManager;
  try {
    manager = await jetstreamManager(connection);
  } catch (error) {
    await connection.close();
    throw error;
  }
  const js = jetstream(connection);
  type PullConsumer = Awaited<ReturnType<typeof js.consumers.get>>;
  let consumer: PullConsumer | undefined;

  const admin: ConsumerAdmin = {
    info: (stream, name) => manager.consumers.info(stream, name),
    add: (stream, config, opts) =>
      manager.consumers.add(stream, config, opts as never),
  };

  const ensure = async (): Promise<EnsureResult> => {
    const result = await ensureDurableConsumer(
      admin,
      options.stream,
      options.name,
      options.subjects,
      options.description,
      options,
    );
    consumer = await js.consumers.get(options.stream, options.name);
    options.onEnsured?.(result);
    return result;
  };

  return {
    ensure,
    async next(expiresMs) {
      if (!consumer) await ensure();
      const message = await (consumer as PullConsumer).next({ expires: expiresMs });
      return message ? toDurableMessage(message) : null;
    },
    flush: () => connection.flush(),
    close: () => connection.close(),
    closed: () => connection.closed(),
  };
}

/** Indirection the trigger goes through, so tests can drive it with a fake backend. */
export const durableTransport: {
  backend: (options: JetStreamBackendOptions) => Promise<DurableBackend>;
} = {
  backend: (options) => jetstreamBackend(options),
};
