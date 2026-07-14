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
  correlationId?: string;
  timeoutMs?: number;
}

const URL_NS = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';

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
  const parts = opts.type.split('.');
  if (parts.length !== 5 || parts[0] !== 'bloodbank' || !parts[1].startsWith('v')) {
    throw new Error(
      `invalid event type "${opts.type}" — need bloodbank.v<N>.<domain>.<entity>.<action>`,
    );
  }
  const domain = parts[2];
  const subject = 'bloodbank.evt.' + opts.type.slice('bloodbank.'.length);
  const tid = String(
    ((opts.data['transcription_id'] as string) ?? (opts.data['id'] as string) ?? '') || '',
  ).trim();
  const correlationid = opts.correlationId || uuid5(tid || opts.type);
  const envelope: Record<string, unknown> = {
    specversion: '1.0',
    id: randomUUID(),
    source: opts.source || 'urn:33god:service:n8n-bloodbank-node',
    type: opts.type,
    subject,
    time: new Date().toISOString(),
    datacontenttype: 'application/json',
    correlationid,
    producer: opts.producer || 'n8n',
    service: opts.service || 'n8n',
    domain,
    schemaref: `${opts.type}.v1`,
    kind: 'event',
    ordering_key: tid ? `transcription:${tid}` : `${domain}:${randomUUID()}`,
    data: opts.data,
  };
  return { subject, envelope };
}

/** Publish an event to NATS in-process via the raw text protocol (no client dep). */
export function publish(opts: EmitOptions): Promise<{ subject: string; correlationid: string }> {
  const { subject, envelope } = buildEnvelope(opts);
  const host = opts.host || '127.0.0.1';
  const port = opts.port || 4222;
  const body = Buffer.from(JSON.stringify(envelope), 'utf8');
  const correlationid = envelope['correlationid'] as string;

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
        resolve({ subject, correlationid });
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
