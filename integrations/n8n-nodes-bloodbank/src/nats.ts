import net from 'node:net';
import { createHash, randomUUID } from 'node:crypto';

export interface EmitOptions {
  /** bloodbank.v<N>.<domain>.<entity>.<action> */
  type: string;
  data: Record<string, unknown>;
  host?: string;
  port?: number;
  source?: string;
  producer?: string;
  service?: string;
  eventId?: string;
  observedAt?: string;
  correlationId?: string;
  causationId?: string;
  orderingKey?: string;
  actor?: Record<string, unknown>;
  traceparent?: string;
  timeoutMs?: number;
}

const URL_NS = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RFC3339_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

/** Deterministic RFC 4122 v5 UUID (stdlib only) so started/completed/failed for one
 *  transcription_id share a correlation id without threading state. */
function uuid5(name: string): string {
  const ns = Buffer.from(URL_NS.replace(/-/g, ''), 'hex');
  const hash = createHash('sha1').update(ns).update(Buffer.from(name, 'utf8')).digest();
  const b = Buffer.from(hash.subarray(0, 16));
  b[6] = (b[6] & 0x0f) | 0x50;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = b.toString('hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function buildEnvelope(opts: EmitOptions): {
  subject: string;
  envelope: Record<string, unknown>;
} {
  const typePattern = /^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;
  if (!typePattern.test(opts.type)) {
    throw new Error(
      `invalid event type "${opts.type}" — need bloodbank.v<N>.<domain>.<entity>.<action>`,
    );
  }
  const parts = opts.type.split('.');
  const domain = parts[2];
  if (domain === 'lifecycle') {
    throw new Error(
      `event type "${opts.type}" is authority-owned by delorenj/lifecycle and cannot be published by n8n`,
    );
  }
  const subject = 'bloodbank.evt.' + opts.type.slice('bloodbank.'.length);
  const transcriptionId = String(opts.data['transcription_id'] ?? '').trim();
  const taskId = String(opts.data['task_id'] ?? '').trim();
  const repo = String(opts.data['repo'] ?? '').trim();
  const entityId = String(opts.data['id'] ?? '').trim();
  const stableEntity = transcriptionId || taskId || entityId;
  const eventId = opts.eventId || randomUUID();
  const observedAt = opts.observedAt || new Date().toISOString();
  if (!RFC3339_PATTERN.test(observedAt) || Number.isNaN(Date.parse(observedAt))) {
    throw new Error(`invalid observedAt "${observedAt}" — need an RFC 3339 timestamp`);
  }
  const correlationid = opts.correlationId || uuid5(stableEntity || eventId);
  const causationid = opts.causationId || eventId;
  for (const [name, value] of Object.entries({ eventId, correlationid, causationid })) {
    if (!UUID_PATTERN.test(value)) {
      throw new Error(`invalid ${name} "${value}" — need an RFC 4122 UUID`);
    }
  }
  let orderingKey = opts.orderingKey;
  if (!orderingKey && transcriptionId) orderingKey = `transcription:${transcriptionId}`;
  if (!orderingKey && taskId) orderingKey = `task:${repo || 'unknown'}:${taskId}`;
  if (!orderingKey && entityId) orderingKey = `${domain}:${entityId}`;
  if (!orderingKey) orderingKey = `${domain}:${eventId}`;
  if (!orderingKey.trim()) throw new Error('orderingKey must not be empty');
  const actor = opts.actor || { type: 'service', agent_id: 'bloodbank.integration.n8n' };
  if (!String(actor['type'] ?? '').trim() || !String(actor['agent_id'] ?? '').trim()) {
    throw new Error('actor.type and actor.agent_id are required');
  }
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
    traceparent:
      opts.traceparent || '00-00000000000000000000000000000000-0000000000000000-00',
    kind: 'event',
    actor,
    ordering_key: orderingKey,
    data: opts.data,
  };
  return { subject, envelope };
}

/** Publish an event to NATS in-process via the raw text protocol (no client dep). */
export function publish(
  opts: EmitOptions,
): Promise<{ subject: string; correlationid: string; eventId: string }> {
  const { subject, envelope } = buildEnvelope(opts);
  const host = opts.host || '127.0.0.1';
  const port = opts.port || 4222;
  const body = Buffer.from(JSON.stringify(envelope), 'utf8');
  const correlationid = envelope['correlationid'] as string;
  const eventId = envelope['id'] as string;

  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    let acc = '';
    let sent = false;
    let done = false;
    const finish = (err?: Error) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      if (err) {
        socket.destroy();
        reject(err);
      } else {
        socket.end();
        resolve({ subject, correlationid, eventId });
      }
    };
    const timer = setTimeout(() => finish(new Error('NATS publish timed out')), opts.timeoutMs ?? 3000);

    socket.on('data', (chunk: Buffer) => {
      acc += chunk.toString('utf8');
      if (!sent && acc.includes('INFO')) {
        sent = true;
        acc = '';
        socket.write('CONNECT {"verbose":false,"pedantic":false,"name":"n8n-bloodbank","lang":"node"}\r\n');
        socket.write(`PUB ${subject} ${body.length}\r\n`);
        socket.write(body);
        socket.write('\r\n');
        socket.write('PING\r\n');
        return;
      }
      if (sent && acc.includes('PONG')) finish();
      if (acc.includes('-ERR')) finish(new Error('NATS -ERR: ' + acc.trim()));
    });
    socket.on('error', (e: Error) => finish(e));
  });
}
