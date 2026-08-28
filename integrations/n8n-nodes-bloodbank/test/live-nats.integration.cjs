const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const test = require('node:test');

const { connect } = require('@nats-io/transport-node');
const {
  BloodbankTrigger,
  publish,
  publishReply,
  subjectFor,
  subscribe,
} = require('../src/index.ts');

const SERVER = process.env.BLOODBANK_NATS_URL || 'nats://localhost:4222';

function deadline(promise, milliseconds = 5000) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      const timer = setTimeout(() => reject(new Error(`timed out after ${milliseconds}ms`)), milliseconds);
      timer.unref();
    }),
  ]);
}

async function publishRaw(subject, envelope) {
  const connection = await connect({ servers: SERVER, name: 'n8n-bloodbank-live-test' });
  try {
    connection.publish(subject, Buffer.from(JSON.stringify(envelope), 'utf8'));
    await connection.flush();
  } finally {
    await connection.drain();
  }
}

function commandEnvelope(type, commandId) {
  return {
    specversion: '1.0',
    id: commandId,
    source: 'urn:33god:test:n8n-bloodbank',
    type,
    subject: subjectFor(type, 'command'),
    time: new Date().toISOString(),
    datacontenttype: 'application/json',
    correlationid: randomUUID(),
    causationid: commandId,
    producer: 'n8n-bloodbank-live-test',
    service: 'n8n-bloodbank-live-test',
    domain: type.split('.')[2],
    kind: 'command',
    actor: { type: 'service', agent_id: 'bloodbank.test.n8n' },
    command_id: commandId,
    idempotency_key: `n8n-live-test:${commandId}`,
    delivery: 'single_consumer',
    reply_to: subjectFor(type, 'reply'),
    timeout_ms: 5000,
    data: { invocation_id: commandId, target_agent_id: 'test-agent' },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function triggerContext(parameters, onEmit) {
  return {
    getNodeParameter(name, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    getNode() {
      return {
        id: 'bloodbank-trigger-live-test',
        name: 'Bloodbank Trigger Live Test',
        type: 'n8n-nodes-bloodbank.bloodbankTrigger',
        typeVersion: 1,
        position: [0, 0],
        parameters: {},
      };
    },
    getMode() {
      return 'trigger';
    },
    getActivationMode() {
      return 'activate';
    },
    emit: onEmit,
    emitError(error) {
      throw error;
    },
    saveFailedExecution(error) {
      throw error;
    },
    helpers: { createDeferredPromise: deferred },
  };
}

test('one trigger subscription can bind multiple event subjects', async () => {
  const expected = new Set();
  let resolveReceived;
  const received = new Promise((resolve) => { resolveReceived = resolve; });
  const subscription = await subscribe({
    host: SERVER,
    subjects: [
      'bloodbank.evt.system.heartbeat.received',
      'bloodbank.evt.repo.task.created',
    ],
    onError: (error) => { throw error; },
    onMessage: async (message) => {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (expected.delete(envelope.id) && expected.size === 0) resolveReceived();
    },
  });
  try {
    const heartbeatId = randomUUID();
    const taskId = randomUUID();
    expected.add(heartbeatId);
    expected.add(taskId);
    const heartbeat = await publish({
      host: SERVER,
      type: 'bloodbank.system.heartbeat.received',
      data: { service: 'n8n-live-test' },
      eventId: heartbeatId,
    });
    const task = await publish({
      host: SERVER,
      type: 'bloodbank.repo.task.created',
      data: { repo: '33GOD', title: 'Live multi-event binding proof' },
      eventId: taskId,
    });
    assert.equal(heartbeat.eventId, heartbeatId);
    assert.equal(task.eventId, taskId);
    await deadline(received);
    assert.equal(expected.size, 0);
  } finally {
    await subscription.close();
  }
});

test('command queue group gives one command to one competing consumer', async () => {
  const type = 'bloodbank.agent.invocation.start';
  const subject = subjectFor(type, 'command');
  const queue = `n8n-live-${randomUUID()}`;
  let deliveries = 0;
  let resolveDelivery;
  const delivered = new Promise((resolve) => { resolveDelivery = resolve; });
  const options = {
    host: SERVER,
    subjects: [subject],
    queue,
    onError: (error) => { throw error; },
    onMessage: async () => {
      deliveries += 1;
      resolveDelivery();
    },
  };
  const first = await subscribe(options);
  const second = await subscribe(options);
  try {
    const commandId = randomUUID();
    await publishRaw(subject, commandEnvelope(type, commandId));
    await deadline(delivered);
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(deliveries, 1);
  } finally {
    await first.close();
    await second.close();
  }
});

test('sync command handling publishes a correlated reply', async () => {
  const type = 'bloodbank.agent.invocation.start';
  const commandSubject = subjectFor(type, 'command');
  const replySubject = subjectFor(type, 'reply');
  const commandId = randomUUID();
  let resolveReply;
  const replyReceived = new Promise((resolve) => { resolveReply = resolve; });
  const replySubscription = await subscribe({
    host: SERVER,
    subjects: [replySubject],
    onError: (error) => { throw error; },
    onMessage: async (message) => {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (envelope.in_reply_to === commandId) resolveReply(envelope);
    },
  });
  const commandSubscription = await subscribe({
    host: SERVER,
    subjects: [commandSubject],
    queue: `n8n-sync-${randomUUID()}`,
    onError: (error) => { throw error; },
    onMessage: async (message) => {
      const command = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (command.command_id === commandId) {
        await publishReply(message.publish, command, 'SUCCESS', { handled: true });
      }
    },
  });
  try {
    const command = commandEnvelope(type, commandId);
    await publishRaw(commandSubject, command);
    const reply = await deadline(replyReceived);
    assert.equal(reply.kind, 'reply');
    assert.equal(reply.status, 'SUCCESS');
    assert.equal(reply.correlationid, command.correlationid);
    assert.equal(reply.causationid, commandId);
    assert.equal(reply.data.handled, true);
  } finally {
    await commandSubscription.close();
    await replySubscription.close();
  }
});

test('BloodbankTrigger class emits every selected event binding', async () => {
  const expected = new Set();
  const observed = new Set();
  let resolveAll;
  const allReceived = new Promise((resolve) => { resolveAll = resolve; });
  const context = triggerContext(
    {
      messageKind: 'event',
      events: [
        'bloodbank.system.heartbeat.received',
        'bloodbank.repo.task.created',
      ],
      connection: { natsHost: SERVER, natsPort: 4222, timeoutMs: 5000 },
    },
    (data) => {
      const id = data[0][0].json.id;
      if (expected.has(id)) observed.add(id);
      if (observed.size === expected.size && expected.size === 2) resolveAll();
    },
  );
  const trigger = await BloodbankTrigger.prototype.trigger.call(context);
  try {
    const firstId = randomUUID();
    const secondId = randomUUID();
    expected.add(firstId);
    expected.add(secondId);
    await publish({
      host: SERVER,
      type: 'bloodbank.system.heartbeat.received',
      data: { service: 'n8n-trigger-class-test' },
      eventId: firstId,
    });
    await publish({
      host: SERVER,
      type: 'bloodbank.repo.task.created',
      data: { repo: '33god', title: 'Trigger class multi-binding proof' },
      eventId: secondId,
    });
    await deadline(allReceived);
    assert.deepEqual(observed, expected);
  } finally {
    await trigger.closeFunction();
  }
});

test('BloodbankTrigger class async mode emits without publishing a reply', async () => {
  const type = 'bloodbank.agent.invocation.start';
  const commandId = randomUUID();
  let resolveEmitted;
  const emitted = new Promise((resolve) => { resolveEmitted = resolve; });
  let replied = false;
  const replySubscription = await subscribe({
    host: SERVER,
    subjects: [subjectFor(type, 'reply')],
    onError: (error) => { throw error; },
    onMessage: async (message) => {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (envelope.in_reply_to === commandId) replied = true;
    },
  });
  const context = triggerContext(
    {
      messageKind: 'command',
      command: type,
      commandProcessing: 'async',
      queueGroup: `n8n-trigger-class-${randomUUID()}`,
      connection: { natsHost: SERVER, natsPort: 4222, timeoutMs: 5000 },
    },
    (data) => {
      if (data[0][0].json.command_id === commandId) resolveEmitted();
    },
  );
  const trigger = await BloodbankTrigger.prototype.trigger.call(context);
  try {
    await publishRaw(subjectFor(type, 'command'), commandEnvelope(type, commandId));
    await deadline(emitted);
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(replied, false);
  } finally {
    await trigger.closeFunction();
    await replySubscription.close();
  }
});

test('BloodbankTrigger class sync mode waits for execution and replies', async () => {
  const type = 'bloodbank.agent.invocation.start';
  const commandId = randomUUID();
  let resolveReply;
  const replyReceived = new Promise((resolve) => { resolveReply = resolve; });
  const replySubscription = await subscribe({
    host: SERVER,
    subjects: [subjectFor(type, 'reply')],
    onError: (error) => { throw error; },
    onMessage: async (message) => {
      const envelope = JSON.parse(Buffer.from(message.data).toString('utf8'));
      if (envelope.in_reply_to === commandId) resolveReply(envelope);
    },
  });
  const context = triggerContext(
    {
      messageKind: 'command',
      command: type,
      commandProcessing: 'sync',
      queueGroup: `n8n-trigger-class-${randomUUID()}`,
      connection: { natsHost: SERVER, natsPort: 4222, timeoutMs: 5000 },
    },
    (_data, _responsePromise, donePromise) => {
      donePromise.resolve({
        data: {
          resultData: {
            runData: {
              Result: [
                {
                  data: {
                    main: [[{ json: { handled: true, source: 'trigger-class-test' } }]],
                  },
                },
              ],
            },
          },
        },
        finished: true,
        mode: 'trigger',
        startedAt: new Date(),
        stoppedAt: new Date(),
        storedAt: 'db',
        status: 'success',
      });
    },
  );
  const trigger = await BloodbankTrigger.prototype.trigger.call(context);
  try {
    const command = commandEnvelope(type, commandId);
    await publishRaw(subjectFor(type, 'command'), command);
    const reply = await deadline(replyReceived);
    assert.equal(reply.status, 'SUCCESS');
    assert.equal(reply.data.execution_status, 'success');
    assert.equal(reply.data.output[0].handled, true);
  } finally {
    await trigger.closeFunction();
    await replySubscription.close();
  }
});
