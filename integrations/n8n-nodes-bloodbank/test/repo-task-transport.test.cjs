'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const EVENT_ID = '10000000-0000-4000-8000-000000000001';
const CORRELATION_ID = '10000000-0000-4000-8000-000000000002';
const CAUSATION_ID = '10000000-0000-4000-8000-000000000003';
const OBSERVED_AT = '2026-07-18T17:00:00.000Z';
const LIFECYCLE_REPO_TASK_FILTER = 'bloodbank.evt.v1.repo.task.>';

const OPTIONS = {
  type: 'bloodbank.v1.repo.task.recorded',
  data: {
    repo: '33GOD',
    task_id: 'TASK-42',
    title: 'Close lifecycle transport contracts',
    change_kind: 'status',
    from: 'in_progress',
    to: 'completed',
    updated_by: 'n8n-contract-test',
    updated_at: OBSERVED_AT,
  },
  source: 'urn:33god:integration:n8n',
  producer: 'n8n',
  service: 'n8n',
  eventId: EVENT_ID,
  observedAt: OBSERVED_AT,
  correlationId: CORRELATION_ID,
  causationId: CAUSATION_ID,
  actor: { type: 'service', agent_id: 'bloodbank.integration.n8n' },
};

function validatorResult(envelope) {
  const hooksPath = path.resolve(__dirname, '../../..', 'services', 'agent-hooks');
  const program = [
    'import json, sys',
    'from core.validate import validate_envelope',
    'validate_envelope(json.load(sys.stdin))',
  ].join('; ');
  return spawnSync('python3', ['-c', program], {
    input: JSON.stringify(envelope),
    encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: hooksPath },
  });
}

function matchesTerminalWildcard(subject, filter) {
  assert.ok(filter.endsWith('>'));
  return subject.startsWith(filter.slice(0, -1));
}

async function fakeNatsServer() {
  let resolvePublished;
  const published = new Promise((resolve) => {
    resolvePublished = resolve;
  });
  const server = net.createServer((socket) => {
    let received = '';
    socket.write('INFO {"server_id":"contract-test"}\r\n');
    socket.on('data', (chunk) => {
      received += chunk.toString('utf8');
      if (!received.includes('PING\r\n')) return;
      const headerStart = received.indexOf('PUB ');
      const headerEnd = received.indexOf('\r\n', headerStart);
      assert.notEqual(headerStart, -1);
      assert.notEqual(headerEnd, -1);
      const [verb, subject, sizeText] = received.slice(headerStart, headerEnd).split(' ');
      assert.equal(verb, 'PUB');
      const size = Number(sizeText);
      const bodyStart = headerEnd + 2;
      const body = received.slice(bodyStart, bodyStart + size);
      resolvePublished({ subject, body });
      socket.write('PONG\r\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    server,
    port: server.address().port,
    published,
  };
}

test('repo.task.recorded builds a deterministic canonical envelope', () => {
  const { buildEnvelope } = require('../dist/nats.js');
  const { eventSchemas } = require('../dist/nodes/Bloodbank/eventSchemas.js');
  assert.ok(eventSchemas.some((schema) => schema.type === OPTIONS.type));
  assert.ok(eventSchemas.every((schema) => schema.domain !== 'lifecycle'));
  assert.throws(
    () => buildEnvelope({ ...OPTIONS, type: 'bloodbank.v1.lifecycle.status.updated' }),
    /authority-owned by delorenj\/lifecycle/,
  );
  assert.throws(
    () => buildEnvelope({ ...OPTIONS, observedAt: '2026-07-18' }),
    /need an RFC 3339 timestamp/,
  );
  assert.throws(
    () => buildEnvelope({ ...OPTIONS, eventId: 'not-a-uuid' }),
    /need an RFC 4122 UUID/,
  );
  const first = buildEnvelope(OPTIONS);
  const second = buildEnvelope(OPTIONS);
  assert.deepEqual(first, second);
  assert.equal(first.subject, 'bloodbank.evt.v1.repo.task.recorded');
  assert.equal(first.envelope.id, EVENT_ID);
  assert.equal(first.envelope.time, OBSERVED_AT);
  assert.equal(first.envelope.causationid, CAUSATION_ID);
  assert.equal(first.envelope.ordering_key, 'task:33GOD:TASK-42');
  assert.equal(
    first.envelope.dataschema,
    'apicurio://holyfields/bloodbank.v1.repo.task.recorded/versions/1',
  );
  const result = validatorResult(first.envelope);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('repo.task.recorded reaches the lifecycle observation transport seam', async (t) => {
  const { publish } = require('../dist/nats.js');
  const fakeNats = await fakeNatsServer();
  t.after(() => fakeNats.server.close());

  const publishResult = await publish({ ...OPTIONS, host: '127.0.0.1', port: fakeNats.port });
  const wire = await fakeNats.published;
  const envelope = JSON.parse(wire.body);

  assert.equal(publishResult.eventId, EVENT_ID);
  assert.equal(wire.subject, 'bloodbank.evt.v1.repo.task.recorded');
  assert.equal(wire.subject, envelope.subject);
  const result = validatorResult(envelope);
  assert.equal(result.status, 0, result.stderr || result.stdout);

  const topologyPath = path.resolve(__dirname, '../../..', 'compose', 'nats', 'streams.json');
  const topology = JSON.parse(fs.readFileSync(topologyPath, 'utf8'));
  const eventsStream = topology.streams.find((stream) => stream.name === 'BLOODBANK_EVENTS');
  assert.ok(eventsStream);
  assert.ok(eventsStream.subjects.includes('bloodbank.evt.v1.>'));
  assert.ok(matchesTerminalWildcard(wire.subject, LIFECYCLE_REPO_TASK_FILTER));

  const daprPath = path.resolve(__dirname, '../../..', 'compose', 'components', 'pubsub.yaml');
  const daprComponent = fs.readFileSync(daprPath, 'utf8');
  assert.match(daprComponent, /name:\s+bloodbank-pubsub/);
  assert.match(daprComponent, /value:\s+"BLOODBANK_EVENTS"/);
});
