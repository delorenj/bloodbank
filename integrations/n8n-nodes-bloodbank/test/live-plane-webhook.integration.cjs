const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const { createHmac, randomUUID } = require('node:crypto');
const test = require('node:test');
const { promisify } = require('node:util');

const { connect } = require('@nats-io/transport-node');

const execFileAsync = promisify(execFile);
const NATS_SERVER = process.env.BLOODBANK_NATS_URL || 'nats://localhost:4222';
const WEBHOOK_URL = process.env.N8N_PLANE_WEBHOOK_URL || 'http://localhost:5678/webhook/plane';
const SECRET_REFERENCE =
  process.env.PLANE_WEBHOOK_SECRET_REFERENCE || 'op://DeLoSecrets/PlaneWebhook/credential';
const BOARD_ID = process.env.PLANE_TEST_BOARD_ID || 'f6746659-7698-4b5a-b509-9666e35fab09';
const PROJECT_SLUG = process.env.PLANE_TEST_PROJECT_SLUG || '33god';

function deadline(promise, milliseconds = 10000) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      const timer = setTimeout(() => reject(new Error(`timed out after ${milliseconds}ms`)), milliseconds);
      timer.unref();
    }),
  ]);
}

test('signed Plane webhook traverses active n8n ingress and publishes canonical provenance', async () => {
  const ticketId = randomUUID();
  const observedAt = new Date().toISOString();
  const payload = {
    event: 'issue',
    action: 'created',
    data: {
      id: ticketId,
      project: BOARD_ID,
      sequence_id: 900001,
      name: '33GOD-38 signed ingress verification',
      created_at: observedAt,
      state_detail: { name: 'In Progress', group: 'started' },
    },
  };
  const raw = JSON.stringify(payload);
  const connection = await connect({
    servers: NATS_SERVER,
    name: 'n8n-plane-webhook-live-test',
  });
  const subscription = connection.subscribe('bloodbank.evt.v1.repo.task.created');
  await connection.flush();
  let resolveEvent;
  const received = new Promise((resolve) => { resolveEvent = resolve; });
  const consume = (async () => {
    for await (const message of subscription) {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (envelope.data?.ticket_id === ticketId) {
        resolveEvent(envelope);
        break;
      }
    }
  })();

  try {
    const { stdout } = await execFileAsync('op', ['read', SECRET_REFERENCE], {
      encoding: 'utf8',
      timeout: 5000,
      maxBuffer: 16 * 1024,
    });
    const secret = stdout.trim();
    assert.ok(secret);
    const signature = createHmac('sha256', secret).update(raw).digest('hex');
    const response = await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-plane-signature': signature,
      },
      body: raw,
    });
    const responseBody = await response.json();
    assert.equal(response.status, 200, JSON.stringify(responseBody));
    assert.equal(responseBody.ok, true);
    assert.equal(responseBody.routed, true);
    assert.equal(responseBody.provider_event_type, 'plane.ticket.created');

    const envelope = await deadline(received);
    assert.equal(envelope.type, 'bloodbank.v1.repo.task.created');
    assert.equal(envelope.subject, 'bloodbank.evt.v1.repo.task.created');
    assert.equal(envelope.source, 'urn:33god:integration:n8n:plane-webhook');
    assert.equal(envelope.producer, 'n8n-plane-webhook');
    assert.equal(envelope.actor.provider, 'plane');
    assert.equal(envelope.workspace, '33god');
    assert.equal(envelope.board_id, BOARD_ID);
    assert.equal(envelope.slug, PROJECT_SLUG);
    assert.equal(envelope.provider_event_type, 'plane.ticket.created');
    assert.equal(envelope.data.ticket_id, ticketId);
    assert.deepEqual(envelope.data.ticket, payload.data);
  } finally {
    subscription.unsubscribe();
    await connection.drain();
    await consume;
  }
});

test('invalid Plane signature is rejected before Bloodbank publication', async () => {
  const ticketId = randomUUID();
  const payload = {
    event: 'issue',
    action: 'created',
    data: {
      id: ticketId,
      project: BOARD_ID,
      sequence_id: 900002,
      name: 'Rejected signature verification',
      created_at: new Date().toISOString(),
    },
  };
  const connection = await connect({
    servers: NATS_SERVER,
    name: 'n8n-plane-webhook-rejection-test',
  });
  const subscription = connection.subscribe('bloodbank.evt.v1.repo.task.created');
  await connection.flush();
  let published = false;
  const consume = (async () => {
    for await (const message of subscription) {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (envelope.data?.ticket_id === ticketId) {
        published = true;
        break;
      }
    }
  })();
  try {
    const response = await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-plane-signature': '0'.repeat(64),
      },
      body: JSON.stringify(payload),
    });
    assert.ok(response.status >= 400);
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(published, false);
  } finally {
    subscription.unsubscribe();
    await connection.drain();
    await consume;
  }
});
