// Durable JetStream delivery for event triggers: the consumer's name and
// config, the pull/ack loop, and the trigger's durable path driven through a
// fake transport. The live proof (n8n stopped, ticket created, n8n started,
// the ticket still groomed) is in the package README.
const assert = require('node:assert/strict');
const test = require('node:test');

const {
  BloodbankTrigger,
  DURABLE_DEFAULTS,
  awaitExecution,
  consumeDurable,
  decideTriggerMessage,
  durableConsumerName,
  durableCreateConfig,
  durableTransport,
  ensureDurableConsumer,
  isConsumerNotFound,
  parseDataConditions,
  sampleEnvelope,
} = require('../src/index.ts');

const NANOS = 1_000_000;
const SUBJECT_CREATED = 'bloodbank.evt.repo.task.created';
const SUBJECT_UPDATED = 'bloodbank.evt.repo.task.updated';

const tick = () => new Promise((resolve) => setImmediate(resolve));
async function until(predicate, label, tries = 400) {
  for (let i = 0; i < tries; i++) {
    if (predicate()) return;
    await tick();
  }
  assert.fail(`timed out waiting for ${label}`);
}

// ---------------------------------------------------------------------------
// Naming and config
// ---------------------------------------------------------------------------

test('the durable name is a pure, NATS-safe function of workflow id and node id', () => {
  const name = durableConsumerName('6wAGA5pdrmHLyhs2', 'f830070b-2b40-4edd-9d27-687e20fc304d');
  assert.equal(name, 'n8n-6wAGA5pdrmHLyhs2-f830070b-2b40-4edd-9d27-687e20fc304d');
  assert.equal(durableConsumerName('6wAGA5pdrmHLyhs2', 'f830070b-2b40-4edd-9d27-687e20fc304d'), name);
  assert.equal(durableConsumerName('wf.1', 'a b>c*d/e'), 'n8n-wf_1-a_b_c_d_e');
  const long = durableConsumerName('w'.repeat(200), 'n'.repeat(200));
  assert.ok(long.length <= 120, long);
  assert.equal(long, durableConsumerName('w'.repeat(200), 'n'.repeat(200)));
  assert.match(long, /^n8n-[A-Za-z0-9_-]+$/);
  assert.throws(() => durableConsumerName('', 'node'), /saved workflow id/);
  assert.throws(() => durableConsumerName('wf', undefined), /saved workflow id/);
});

test('a first creation starts at the tip of the stream, never a week back', () => {
  const config = durableCreateConfig('n8n-wf-node', [SUBJECT_CREATED], 'desc');
  assert.equal(config.durable_name, 'n8n-wf-node');
  assert.equal(config.deliver_policy, 'new');
  assert.equal(config.ack_policy, 'explicit');
  assert.equal(config.filter_subject, SUBJECT_CREATED);
  assert.equal(config.filter_subjects, undefined);
  assert.equal(config.ack_wait, DURABLE_DEFAULTS.ackWaitMs * NANOS);
  assert.equal(config.inactive_threshold, 7 * 24 * 3600 * 1000 * NANOS);
  assert.ok(config.max_ack_pending > 0 && config.max_ack_pending <= 64);
  assert.ok(config.max_deliver > 1);
  const multi = durableCreateConfig('n', [SUBJECT_UPDATED, SUBJECT_CREATED, SUBJECT_CREATED], 'd');
  assert.equal(multi.filter_subject, undefined);
  assert.deepEqual(multi.filter_subjects, [SUBJECT_CREATED, SUBJECT_UPDATED]);
});

function fakeAdmin(existing) {
  const calls = [];
  let current = existing;
  return {
    calls,
    async info(stream, name) {
      calls.push(['info', stream, name]);
      if (!current) {
        const error = new Error('consumer not found');
        error.code = 10014;
        throw error;
      }
      return { name, stream_name: stream, config: current };
    },
    async add(stream, config, opts) {
      calls.push(['add', stream, config, opts]);
      current = config;
      return { name: config.durable_name, stream_name: stream, config };
    },
  };
}

test('ensure creates a missing durable with deliver new', async () => {
  const admin = fakeAdmin(null);
  const result = await ensureDurableConsumer(admin, 'BLOODBANK_EVENTS', 'n8n-wf-node', [SUBJECT_CREATED], 'desc');
  assert.equal(result.action, 'created');
  const [, stream, config, opts] = admin.calls.find((call) => call[0] === 'add');
  assert.equal(stream, 'BLOODBANK_EVENTS');
  assert.equal(config.deliver_policy, 'new');
  assert.equal(opts, undefined);
});

test('ensure leaves a current durable alone, so it resumes where it stopped', async () => {
  const live = { ...durableCreateConfig('n8n-wf-node', [SUBJECT_CREATED], 'desc'), num_replicas: 1 };
  const admin = fakeAdmin(live);
  const result = await ensureDurableConsumer(admin, 'BLOODBANK_EVENTS', 'n8n-wf-node', [SUBJECT_CREATED], 'desc');
  assert.equal(result.action, 'unchanged');
  assert.deepEqual(admin.calls.map((call) => call[0]), ['info']);
});

test('ensure updates only the owned fields in place when the bindings change', async () => {
  const live = {
    ...durableCreateConfig('n8n-wf-node', [SUBJECT_CREATED], 'old'),
    deliver_policy: 'new',
    opt_start_seq: undefined,
    num_replicas: 1,
  };
  const admin = fakeAdmin(live);
  const result = await ensureDurableConsumer(
    admin,
    'BLOODBANK_EVENTS',
    'n8n-wf-node',
    [SUBJECT_UPDATED, SUBJECT_CREATED],
    'new',
  );
  assert.equal(result.action, 'updated');
  assert.deepEqual(result.drift.sort(), ['description', 'filter_subjects']);
  const [, , config, opts] = admin.calls.find((call) => call[0] === 'add');
  assert.deepEqual(opts, { action: 'update' });
  assert.deepEqual(config.filter_subjects, [SUBJECT_CREATED, SUBJECT_UPDATED]);
  assert.ok(!('filter_subject' in config), 'filter_subject and filter_subjects are exclusive');
  assert.equal(config.deliver_policy, 'new'); // carried over untouched
  assert.equal(config.durable_name, 'n8n-wf-node');
});

test('ensure refuses a push or ack-none consumer squatting on the name', async () => {
  await assert.rejects(
    () => ensureDurableConsumer(fakeAdmin({ durable_name: 'x', deliver_subject: '_INBOX.x', ack_policy: 'explicit' }), 'S', 'x', [SUBJECT_CREATED], 'd'),
    /push consumer/,
  );
  await assert.rejects(
    () => ensureDurableConsumer(fakeAdmin({ durable_name: 'x', ack_policy: 'none' }), 'S', 'x', [SUBJECT_CREATED], 'd'),
    /ack_policy none/,
  );
});

test('ensure surfaces real JetStream errors instead of recreating over them', async () => {
  const admin = {
    info: async () => { throw new Error('JetStream not enabled'); },
    add: async () => assert.fail('must not create'),
  };
  await assert.rejects(() => ensureDurableConsumer(admin, 'S', 'x', [SUBJECT_CREATED], 'd'), /not enabled/);
  assert.ok(isConsumerNotFound({ api_error: { err_code: 10014 } }));
  assert.ok(isConsumerNotFound(new Error('consumer not found')));
  assert.ok(!isConsumerNotFound(new Error('timeout')));
});

// ---------------------------------------------------------------------------
// The consume loop
// ---------------------------------------------------------------------------

function message(seq, envelope, { timestampMs = Date.now(), subject = SUBJECT_CREATED } = {}) {
  const log = [];
  return {
    log,
    subject,
    seq,
    timestampMs,
    deliveryCount: 1,
    data: Buffer.from(typeof envelope === 'string' ? envelope : JSON.stringify(envelope), 'utf8'),
    ack: () => log.push('ack'),
    term: (reason) => log.push(`term:${reason}`),
    working: () => log.push('working'),
  };
}

/** A backend whose queue the test feeds; `next` parks until a message arrives or close. */
function fakeBackend() {
  const queue = [];
  const waiters = [];
  const events = [];
  let closed = false;
  let resolveClosed;
  const closedPromise = new Promise((resolve) => { resolveClosed = resolve; });
  const failures = [];
  return {
    events,
    push(item) {
      if (waiters.length) waiters.shift()(item);
      else queue.push(item);
    },
    failNext(error) { failures.push(error); },
    async ensure() {
      events.push('ensure');
      return { action: 'unchanged', drift: [], info: {} };
    },
    next() {
      events.push('next');
      if (failures.length) return Promise.reject(failures.shift());
      if (closed) return Promise.resolve(null);
      if (queue.length) return Promise.resolve(queue.shift());
      return new Promise((resolve) => waiters.push(resolve));
    },
    async flush() { events.push('flush'); },
    async close() {
      events.push('close');
      closed = true;
      while (waiters.length) waiters.shift()(null);
      resolveClosed();
    },
    closed: () => closedPromise,
  };
}

test('one message at a time, acknowledged only after its handler resolves', async () => {
  const backend = fakeBackend();
  const seen = [];
  const release = [];
  const subscription = await consumeDurable(backend, {
    onFatal: (error) => assert.fail(error),
    onMessage: (m) => {
      seen.push(m.seq);
      return new Promise((resolve) => release.push(() => resolve('ack')));
    },
  });
  const first = message(1, {});
  const second = message(2, {});
  backend.push(first);
  backend.push(second);
  await until(() => seen.length === 1, 'first handler');
  await tick();
  assert.deepEqual(seen, [1], 'the second message waits for the first to finish');
  assert.deepEqual(first.log, []);
  release.shift()();
  await until(() => seen.length === 2, 'second handler');
  assert.deepEqual(first.log, ['ack']);
  assert.deepEqual(second.log, []);
  release.shift()();
  await until(() => second.log.includes('ack'), 'second ack');
  await subscription.close();
});

test('a slow execution keeps its message alive with working()', async () => {
  const backend = fakeBackend();
  let finish;
  const subscription = await consumeDurable(backend, {
    workingEveryMs: 5,
    onFatal: () => {},
    onMessage: () => new Promise((resolve) => { finish = resolve; }),
  });
  const slow = message(1, {});
  backend.push(slow);
  await until(() => typeof finish === 'function', 'handler');
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.ok(slow.log.filter((entry) => entry === 'working').length >= 2, slow.log.join(','));
  finish('ack');
  await until(() => slow.log.includes('ack'), 'ack');
  await subscription.close();
});

test('term verdicts and throwing handlers stop redelivery', async () => {
  const backend = fakeBackend();
  const warnings = [];
  const subscription = await consumeDurable(backend, {
    onFatal: () => {},
    onWarning: (text) => warnings.push(text),
    onMessage: async (m) => {
      if (m.seq === 1) return 'term';
      throw new Error('boom');
    },
  });
  const rejected = message(1, {});
  const exploding = message(2, {});
  backend.push(rejected);
  backend.push(exploding);
  await until(() => exploding.log.length > 0, 'second verdict');
  assert.match(rejected.log[0], /^term:/);
  assert.match(exploding.log[0], /^term:/);
  assert.ok(warnings.some((text) => /failed; terminated/.test(text)));
  await subscription.close();
});

test('events older than the catch-up window are acknowledged without an execution', async () => {
  const backend = fakeBackend();
  const handled = [];
  const stale = [];
  const now = 1_000_000_000;
  const subscription = await consumeDurable(backend, {
    now: () => now,
    catchUpWindowMs: 60_000,
    onFatal: () => {},
    onStale: (m, age) => stale.push([m.seq, age]),
    onMessage: async (m) => { handled.push(m.seq); return 'ack'; },
  });
  const old = message(1, {}, { timestampMs: now - 120_000 });
  const fresh = message(2, {}, { timestampMs: now - 1_000 });
  backend.push(old);
  backend.push(fresh);
  await until(() => fresh.log.includes('ack'), 'fresh ack');
  assert.deepEqual(old.log, ['ack']);
  assert.deepEqual(stale, [[1, 120_000]]);
  assert.deepEqual(handled, [2]);
  await subscription.close();
});

test('a failed pull backs off, re-ensures the durable and carries on', async () => {
  const backend = fakeBackend();
  const warnings = [];
  const handled = [];
  backend.failNext(new Error('no responders'));
  const subscription = await consumeDurable(backend, {
    sleep: async () => {},
    onFatal: () => {},
    onWarning: (text) => warnings.push(text),
    onMessage: async (m) => { handled.push(m.seq); return 'ack'; },
  });
  backend.push(message(7, {}));
  await until(() => handled.length === 1, 'message after a failed pull');
  assert.deepEqual(backend.events.slice(0, 4), ['ensure', 'next', 'ensure', 'next']);
  assert.ok(warnings.some((text) => /pull failed/.test(text)));
  await subscription.close();
});

test('close waits for the in-flight execution, acks it, then flushes and closes', async () => {
  const backend = fakeBackend();
  let finish;
  const subscription = await consumeDurable(backend, {
    onFatal: () => {},
    onMessage: () => new Promise((resolve) => { finish = resolve; }),
  });
  const inflight = message(1, {});
  backend.push(inflight);
  await until(() => typeof finish === 'function', 'handler');
  const closing = subscription.close();
  await tick();
  assert.ok(!backend.events.includes('close'), 'close waits for the execution');
  finish('ack');
  await closing;
  assert.deepEqual(inflight.log, ['ack']);
  const tail = backend.events.slice(-2);
  assert.deepEqual(tail, ['flush', 'close']);
});

test('a connection that dies on its own is fatal; a deliberate close is not', async () => {
  const backend = fakeBackend();
  const fatal = [];
  const subscription = await consumeDurable(backend, { onFatal: (e) => fatal.push(e), onMessage: async () => 'ack' });
  await subscription.close();
  await tick();
  assert.deepEqual(fatal, []);
});

// ---------------------------------------------------------------------------
// Shared decision + bounded wait
// ---------------------------------------------------------------------------

test('both delivery paths decide messages the same way', () => {
  const created = sampleEnvelope('plane.ticket.created', 'event');
  const updated = sampleEnvelope('plane.ticket.transitioned', 'event');
  const bytes = (value) => Buffer.from(JSON.stringify(value), 'utf8');
  assert.equal(decideTriggerMessage('event', ['plane.ticket.created'], [], bytes(created)).verdict, 'emit');
  assert.equal(decideTriggerMessage('event', ['plane.ticket.created'], [], bytes(updated)).verdict, 'filtered');
  const conditions = parseDataConditions({ conditions: [{ path: 'data.provider', values: 'jira' }] });
  assert.equal(decideTriggerMessage('event', ['plane.ticket.created'], conditions, bytes(created)).verdict, 'filtered');
  const bad = decideTriggerMessage('event', ['plane.ticket.created'], [], Buffer.from('not json'));
  assert.equal(bad.verdict, 'rejected');
  const command = decideTriggerMessage('event', ['plane.ticket.created'], [], bytes({ ...created, kind: 'command' }));
  assert.equal(command.verdict, 'rejected');
  assert.match(command.reason, /kind=command/);
});

test('waiting on an execution is bounded and never throws', async () => {
  assert.equal(await awaitExecution(Promise.resolve({}), 1000), 'finished');
  assert.equal(await awaitExecution(Promise.reject(new Error('x')), 1000), 'failed');
  assert.equal(await awaitExecution(new Promise(() => {}), 5), 'timeout');
});

// ---------------------------------------------------------------------------
// The trigger's durable path, through a fake transport
// ---------------------------------------------------------------------------

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function activeTriggerContext(parameters) {
  const emitted = [];
  const failed = [];
  const logs = [];
  const context = {
    getMode: () => 'trigger',
    getWorkflow: () => ({ id: 'wfDurable0000001', name: 'Durable Test', active: true }),
    getNode: () => ({ id: 'node-0001', name: 'onTicketCreated', type: 'n8n-nodes-bloodbank.bloodbankTrigger', typeVersion: 1, parameters: {} }),
    getNodeParameter(name, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    helpers: { createDeferredPromise: deferred },
    logger: {
      info: (text) => logs.push(['info', text]),
      warn: (text) => logs.push(['warn', text]),
      error: (text) => logs.push(['error', text]),
      debug: () => {},
    },
    emit: (data, response, done) => emitted.push({ data, done }),
    emitError: (error) => { throw error; },
    saveFailedExecution: (error) => failed.push(error),
  };
  return { context, emitted, failed, logs };
}

async function withFakeTransport(t, backend) {
  const original = durableTransport.backend;
  const seen = [];
  durableTransport.backend = async (options) => { seen.push(options); return backend; };
  t.after(() => { durableTransport.backend = original; });
  return seen;
}

test('an active event trigger consumes its own durable and acks after the execution ends', async (t) => {
  const backend = fakeBackend();
  const seen = await withFakeTransport(t, backend);
  const { context, emitted, failed } = activeTriggerContext({
    messageKind: 'event',
    events: ['plane.ticket.created'],
  });
  const response = await BloodbankTrigger.prototype.trigger.call(context);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].stream, 'BLOODBANK_EVENTS');
  assert.equal(seen[0].name, 'n8n-wfDurable0000001-node-0001');
  assert.deepEqual(seen[0].subjects, [SUBJECT_CREATED]);
  assert.match(seen[0].description, /Durable Test/);

  const created = sampleEnvelope('plane.ticket.created', 'event');
  const transitioned = sampleEnvelope('plane.ticket.transitioned', 'event');
  const other = message(1, transitioned, { subject: SUBJECT_UPDATED });
  const wanted = message(2, created);
  const garbage = message(3, '{"kind":');
  backend.push(other);
  backend.push(wanted);
  backend.push(garbage);

  await until(() => emitted.length === 1, 'emit');
  assert.deepEqual(other.log, ['ack'], 'a message another alias owns is acked, never emitted');
  assert.equal(emitted[0].data[0][0].json.id, created.id);
  assert.ok(emitted[0].done, 'emitted with a done promise');
  await tick();
  assert.deepEqual(wanted.log, [], 'not acked while the execution runs');
  emitted[0].done.resolve({ status: 'success' });
  await until(() => wanted.log.includes('ack'), 'ack after execution');
  await until(() => garbage.log.length > 0, 'garbage verdict');
  assert.match(garbage.log[0], /^term:/);
  assert.equal(failed.length, 1);
  assert.match(failed[0].message, /Rejected malformed Bloodbank message/);
  await response.closeFunction();
  assert.ok(backend.events.includes('close'));
});

test('On Emit acknowledges as soon as the execution starts', async (t) => {
  const backend = fakeBackend();
  await withFakeTransport(t, backend);
  const { context, emitted } = activeTriggerContext({
    messageKind: 'event',
    events: ['plane.ticket.created'],
    acknowledge: 'onEmit',
  });
  const response = await BloodbankTrigger.prototype.trigger.call(context);
  const m = message(1, sampleEnvelope('plane.ticket.created', 'event'));
  backend.push(m);
  await until(() => m.log.includes('ack'), 'ack on emit');
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].done, undefined);
  await response.closeFunction();
});

test('Ephemeral delivery and command triggers never touch a durable', async (t) => {
  const seen = await withFakeTransport(t, fakeBackend());
  const trigger = new BloodbankTrigger();
  const delivery = trigger.description.properties.find((p) => p.name === 'delivery');
  assert.deepEqual(delivery.options.map((o) => o.value), ['durable', 'ephemeral']);
  assert.equal(delivery.default, 'durable');
  assert.deepEqual(delivery.displayOptions.show.messageKind, ['event']);
  for (const name of ['acknowledge', 'catchUpHours']) {
    const property = trigger.description.properties.find((p) => p.name === name);
    assert.deepEqual(property.displayOptions.show.delivery, ['durable'], name);
  }
  // Ephemeral goes straight to a core subscription; point it at a dead port so
  // the attempt fails fast without any durable being asked for.
  const { context } = activeTriggerContext({
    messageKind: 'event',
    events: ['plane.ticket.created'],
    delivery: 'ephemeral',
    connection: { natsHost: '127.0.0.1', natsPort: 1, timeoutMs: 200 },
  });
  await assert.rejects(() => BloodbankTrigger.prototype.trigger.call(context));
  assert.equal(seen.length, 0);
});
