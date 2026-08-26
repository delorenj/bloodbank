import { createHash, randomUUID } from 'node:crypto';

import {
  connect,
} from '@nats-io/transport-node';

type NatsConnection = Awaited<ReturnType<typeof connect>>;
type Subscription = ReturnType<NatsConnection['subscribe']>;

export interface NatsConnectionOptions {
  host?: string;
  port?: number;
  timeoutMs?: number;
}

export interface EmitOptions extends NatsConnectionOptions {
  /** bloodbank.v<N>.<domain>.<entity>.<action> */
  type: string;
  data: Record<string, unknown>;
  source?: string;
  producer?: string;
  service?: string;
  eventId?: string;
  observedAt?: string;
  correlationId?: string;
  causationId?: string | null;
  orderingKey?: string;
  actor?: Record<string, unknown>;
  extensions?: Record<string, string | number | boolean | null>;
}

export interface IncomingNatsMessage {
  subject: string;
  data: Uint8Array;
  replySubject?: string;
  publish(subject: string, envelope: Record<string, unknown>): Promise<void>;
}

export interface SubscribeOptions extends NatsConnectionOptions {
  subjects: string[];
  queue?: string;
  name?: string;
  onMessage(message: IncomingNatsMessage): Promise<void>;
  onError(error: Error): void;
}

export interface BloodbankSubscription {
  close(): Promise<void>;
}

const URL_NS = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';
const TYPE_PATTERN =
  /^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;
const RESERVED_ENVELOPE_KEYS = new Set([
  'specversion',
  'id',
  'source',
  'type',
  'subject',
  'time',
  'datacontenttype',
  'dataschema',
  'correlationid',
  'causationid',
  'producer',
  'service',
  'domain',
  'schemaref',
  'traceparent',
  'kind',
  'actor',
  'ordering_key',
  'data',
]);

/** Deterministic RFC 4122 v5 UUID for retry-stable provider observations. */
export function deterministicUuid(name: string): string {
  const ns = Buffer.from(URL_NS.replace(/-/g, ''), 'hex');
  const hash = createHash('sha1').update(ns).update(Buffer.from(name, 'utf8')).digest();
  const bytes = Buffer.from(hash.subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function typeParts(type: string): [string, string, string, string, string] {
  if (!TYPE_PATTERN.test(type)) {
    throw new Error(
      `invalid Bloodbank type "${type}" — need bloodbank.v<N>.<domain>.<entity>.<action>`,
    );
  }
  return type.split('.') as [string, string, string, string, string];
}

function serverUrl(host = 'localhost', port = 4222): string {
  if (host.includes('://')) return host;
  return `nats://${host}:${port}`;
}

function entityIdentity(data: Record<string, unknown>): string {
  for (const key of ['transcription_id', 'task_id', 'ticket_id', 'board_id', 'id']) {
    const value = data[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

export function subjectFor(type: string, kind: 'event' | 'command' | 'reply'): string {
  typeParts(type);
  const marker = kind === 'event' ? 'evt' : kind === 'command' ? 'cmd' : 'rpy';
  return `bloodbank.${marker}.${type.slice('bloodbank.'.length)}`;
}

export function buildEnvelope(opts: EmitOptions): {
  subject: string;
  envelope: Record<string, unknown>;
} {
  const [, , domain] = typeParts(opts.type);
  const subject = subjectFor(opts.type, 'event');
  const identity = entityIdentity(opts.data);
  const eventId = opts.eventId || randomUUID();
  const correlationid =
    opts.correlationId || deterministicUuid(`${opts.type}:${identity || eventId}`);
  const causationid = opts.causationId === undefined ? eventId : opts.causationId;
  const observedAt = opts.observedAt || new Date().toISOString();
  const orderingKey = opts.orderingKey || (identity ? `${domain}:${identity}` : `${domain}:${eventId}`);
  const actor = opts.actor || { type: 'service', agent_id: 'bloodbank.integration.n8n' };
  const envelope: Record<string, unknown> = {
    specversion: '1.0',
    id: eventId,
    source: opts.source || 'urn:33god:service:n8n-bloodbank-node',
    type: opts.type,
    subject,
    time: observedAt,
    datacontenttype: 'application/json',
    dataschema: `apicurio://holyfields/${opts.type}/versions/1`,
    correlationid,
    causationid,
    producer: opts.producer || 'n8n',
    service: opts.service || 'n8n',
    domain,
    schemaref: `${opts.type}.v1`,
    traceparent: '00-00000000000000000000000000000000-0000000000000000-00',
    kind: 'event',
    actor,
    ordering_key: orderingKey,
  };
  for (const [key, value] of Object.entries(opts.extensions || {})) {
    if (RESERVED_ENVELOPE_KEYS.has(key)) {
      throw new Error(`extension "${key}" cannot replace a canonical envelope field`);
    }
    envelope[key] = value;
  }
  envelope.data = opts.data;
  return { subject, envelope };
}

function replyEnvelope(
  command: Record<string, unknown>,
  status: 'SUCCESS' | 'ERROR',
  data: Record<string, unknown>,
): { subject: string; envelope: Record<string, unknown> } {
  const type = String(command.type || '');
  const [, , domain] = typeParts(type);
  const subject = subjectFor(type, 'reply');
  const replyId = randomUUID();
  const commandId = String(command.command_id || command.id || '');
  if (!commandId) throw new Error('cannot reply to a command without command_id or id');
  const correlationid =
    typeof command.correlationid === 'string' && command.correlationid
      ? command.correlationid
      : deterministicUuid(`command:${commandId}`);
  return {
    subject,
    envelope: {
      specversion: '1.0',
      id: replyId,
      source: 'urn:33god:service:n8n-bloodbank-trigger',
      type,
      subject,
      time: new Date().toISOString(),
      datacontenttype: 'application/json',
      dataschema: `apicurio://holyfields/${type}/versions/1`,
      correlationid,
      causationid: commandId,
      producer: 'n8n',
      service: 'n8n',
      domain,
      schemaref: `${type}.v1`,
      traceparent: '00-00000000000000000000000000000000-0000000000000000-00',
      kind: 'reply',
      actor: { type: 'service', agent_id: 'bloodbank.integration.n8n-trigger' },
      reply_id: replyId,
      in_reply_to: commandId,
      status,
      data,
    },
  };
}

async function publishOnConnection(
  connection: NatsConnection,
  subject: string,
  envelope: Record<string, unknown>,
): Promise<void> {
  connection.publish(subject, Buffer.from(JSON.stringify(envelope), 'utf8'));
  await connection.flush();
}

export async function publishReply(
  publishMessage: IncomingNatsMessage['publish'],
  command: Record<string, unknown>,
  status: 'SUCCESS' | 'ERROR',
  data: Record<string, unknown>,
  transportReplySubject?: string,
): Promise<{ subject: string; replyId: string }> {
  const reply = replyEnvelope(command, status, data);
  const declaredReply =
    typeof command.reply_to === 'string' && command.reply_to.trim()
      ? command.reply_to.trim()
      : undefined;
  const destination = transportReplySubject || declaredReply || reply.subject;
  await publishMessage(destination, reply.envelope);
  return { subject: destination, replyId: String(reply.envelope.reply_id) };
}

/** Publish a canonical event through the official NATS client. */
export async function publish(
  opts: EmitOptions,
): Promise<{ subject: string; correlationid: string; eventId: string }> {
  const { subject, envelope } = buildEnvelope(opts);
  const connection = await connect({
    servers: serverUrl(opts.host, opts.port),
    name: 'n8n-bloodbank-publisher',
    timeout: opts.timeoutMs ?? 3000,
  });
  try {
    await publishOnConnection(connection, subject, envelope);
  } finally {
    await connection.drain();
  }
  return {
    subject,
    correlationid: String(envelope.correlationid),
    eventId: String(envelope.id),
  };
}

/** Open reconnecting Core NATS subscriptions for an n8n trigger workflow. */
export async function subscribe(opts: SubscribeOptions): Promise<BloodbankSubscription> {
  const subjects = [...new Set(opts.subjects.filter(Boolean))];
  if (!subjects.length) throw new Error('at least one NATS subject is required');
  const connection = await connect({
    servers: serverUrl(opts.host, opts.port),
    name: opts.name || 'n8n-bloodbank-trigger',
    timeout: opts.timeoutMs ?? 5000,
    reconnect: true,
    maxReconnectAttempts: -1,
  });
  let closing = false;
  const subscriptions: Subscription[] = [];

  const consume = async (subscription: Subscription): Promise<void> => {
    try {
      for await (const message of subscription) {
        await opts.onMessage({
          subject: message.subject,
          data: message.data,
          replySubject: message.reply || undefined,
          publish: (subject, envelope) => publishOnConnection(connection, subject, envelope),
        });
      }
    } catch (error) {
      if (!closing) opts.onError(error instanceof Error ? error : new Error(String(error)));
    }
  };

  for (const subject of subjects) {
    const subscription = connection.subscribe(
      subject,
      opts.queue ? { queue: opts.queue } : undefined,
    );
    subscriptions.push(subscription);
    void consume(subscription);
  }
  await connection.flush();

  void connection.closed().then((error?: Error) => {
    if (!closing && error) opts.onError(error);
  });

  return {
    async close(): Promise<void> {
      if (closing) return;
      closing = true;
      for (const subscription of subscriptions) subscription.unsubscribe();
      await connection.drain();
    },
  };
}
