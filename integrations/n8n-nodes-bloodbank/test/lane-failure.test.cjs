// A failed ticket-lifecycle lane run as a validated bloodbank.workflow.task.failed
// fact (0.8.0, Fleet "Report Lane Failure"). Every fixture is hand-written and
// sanitized from the shapes recon saw on 2026-11-05 (Plane 429 on the groomed
// lane, Plane 500 on the pickup chip's sweep); none of it is a raw execution.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  DEFAULT_LANE_MAP,
  Fleet,
  buildEnvelope,
  companyIdentityOf,
  parseLaneMap,
  publish,
  redactText,
  reportLaneFailure,
  slugify,
  validateEnvelope,
} = require('../src/index.ts');

const TYPE = 'bloodbank.workflow.task.failed';
const COMMAND = 'bloodbank.agent.invocation.start';

const WF_GROOMING = '6wAGA5pdrmHLyhs2';
const WF_DELEGATION = '8mmqdMwQYA28ZwUj';
const WF_GROOMED = 'l0J53w0TsS5PwETk';
const WF_CHIP = 'wWXgCZiiIBWaRRzE';

const HEYMA_BOARD = '7485039f-85b0-47db-ad99-d11497f043d7';
const HEYMA_TICKET = 'a6e2a0bd-75f3-435a-91e3-1bac97d92553';
const JIMB_BOARD = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const JIMB_TICKET = '5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd';
const GRUV_BOARD = 'fdfbeb2d-4e0f-43a3-9615-f3f13c60a9f1';
const SUBJECT = '0f1e2d3c-4b5a-4968-8777-665544332211';
const TRIGGER_EVENT = '6c523e67-6357-55b3-9ce3-b5b45f564762';
const TRIGGER_CORRELATION = '65af4a09-f7bb-5ed7-80a2-72675e30a880';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function agentRow(id, board, workspace, identifier, extra = {}) {
  return {
    repo: id.replace(/-pm$/, ''),
    profile_name: id,
    project_path: `/nonexistent/${id}`,
    plane: { identifier, project_id: board, workspace },
    bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: id },
    ...extra,
  };
}

/** heyma-pm is an enrolled employee (grolf-like); james-brennan-pm is not. */
const REGISTRY = {
  schema_version: 1,
  agents: {
    'heyma-pm': agentRow('heyma-pm', HEYMA_BOARD, '33god', 'HEYMA', {
      repo: 'transcription-queue',
      gateway_consumer: 'hermes-33god-pm',
      company_identity: {
        subject: SUBJECT,
        handle: 'grolf',
        email: 'grolf@agents.delo.sh',
        status: 'active',
        plane: { attribution_mode: 'native', native_actor_id: '11111111-2222-4333-8444-555555555555' },
      },
    }),
    'james-brennan-pm': agentRow('james-brennan-pm', JIMB_BOARD, 'automaticai', 'JIMB', {
      repo: 'james-brennan',
      gateway_consumer: 'hermes',
    }),
  },
};

function planeNode(name) {
  return {
    name,
    type: 'n8n-nodes-base.httpRequest',
    parameters: { url: '=https://plane.delo.sh/api/v1/workspaces/{{ $json.ws }}/projects/{{ $json.board }}/issues/{{ $json.ticketId }}/' },
  };
}

const runItem = (...jsons) => [{ data: { main: [jsons.map((json) => ({ json }))] } }];

function createdEnvelope(overrides = {}) {
  return {
    id: TRIGGER_EVENT,
    type: 'bloodbank.repo.task.created',
    correlationid: TRIGGER_CORRELATION,
    data: {
      ticket_key: 'JIMB-9',
      ticket_id: JIMB_TICKET,
      board_id: JIMB_BOARD,
      workspace: 'automaticai',
      repo: 'james-brennan',
      phase: 'Backlog',
      title: 'Wire the lane',
      provider_event_type: 'plane.ticket.created',
      ticket: { labels: [{ name: 'bug' }] },
      ...overrides,
    },
  };
}

const GROOMING_ERROR = {
  execution: {
    id: '640001',
    url: `https://n8n.delo.sh/workflow/${WF_GROOMING}/executions/640001`,
    lastNodeExecuted: 'Groom Ticket',
    mode: 'trigger',
    retryOf: null,
    error: { message: 'Cannot read the fleet registry at /x/agents-registry.yaml: ENOENT', stack: 'Error: Cannot read the fleet registry\n    at /home/someone/.local/share/n8n/node.js:1:1' },
  },
  workflow: { id: WF_GROOMING, name: 'Ticket Grooming' },
};

const GROOMING_LOOKUP = {
  id: '640001',
  stoppedAt: '2026-11-05T19:04:42.255Z',
  workflowData: { id: WF_GROOMING, name: 'Ticket Grooming', nodes: [] },
  data: { resultData: { runData: { onTicketCreated: runItem(createdEnvelope()) } } },
};

const GROOMED_ERROR = {
  execution: {
    id: '632616',
    url: `https://n8n.delo.sh/workflow/${WF_GROOMED}/executions/632616`,
    lastNodeExecuted: 'Groomed in Todo?',
    mode: 'trigger',
    retryOf: null,
    error: {
      message: "HTTP 429 Try spacing your requests out using the batching settings under 'Options' [line 40]",
      description: 'Read Ticket failed for HEYMA-12 (board 7485039f-85b0-47db-ad99-d11497f043d7)',
      stack: `Error: Read Ticket failed for HEYMA-12 (board ${HEYMA_BOARD}): HTTP 429 Try spacing your requests\n    at failure (evalmachine.<anonymous>:40:10)`,
    },
  },
  workflow: { id: WF_GROOMED, name: 'Ticket Delegation — Groomed in Todo' },
};

const GROOMED_LOOKUP = {
  id: '632616',
  stoppedAt: '2026-11-05T19:04:42.255Z',
  workflowData: {
    id: WF_GROOMED,
    name: 'Ticket Delegation — Groomed in Todo',
    nodes: [planeNode('Read Ticket'), { name: 'Groomed in Todo?', type: 'n8n-nodes-base.code', parameters: {} }],
  },
  data: {
    resultData: {
      runData: {
        onGroomingCompleted: runItem({
          id: TRIGGER_EVENT,
          type: 'bloodbank.agent.invocation.completed',
          correlationid: TRIGGER_CORRELATION,
          data: {
            target_agent_id: 'heyma-pm',
            context: {
              reason: 'ticket-grooming',
              repo: 'transcription-queue',
              ticket_key: 'HEYMA-12',
              ticket_id: HEYMA_TICKET,
              board_id: HEYMA_BOARD,
              workspace: '33god',
              title: '[wax] pass call-metadata: call_not_found',
              phase: 'Todo',
              previous_phase: '',
              provider_event_type: 'plane.ticket.created',
            },
          },
        }),
        'Read Ticket': runItem({ labels: [{ name: 'lifecycle:triaged' }, { name: 'bug' }] }),
      },
    },
  },
};

const CHIP_ERROR = {
  execution: {
    id: '637574',
    url: `https://n8n.delo.sh/workflow/${WF_CHIP}/executions/637574`,
    lastNodeExecuted: 'Chip — Resolve Label',
    mode: 'trigger',
    retryOf: null,
    error: {
      // n8n splits the message at the last ": ", leaving a useless tail.
      message: '\\"Something went wrong please try again later\\"}" [line 27]',
      description: 'HTTP 500 500 - "{\\"error"',
      stack:
        `Error: Chip — List Labels failed for GRUV-147 (board ${GRUV_BOARD}): HTTP 500 500 - "{\\"error\\":\\"Something went wrong\\"}"\n    at failure (evalmachine.<anonymous>:27:10)`,
    },
  },
  workflow: { id: WF_CHIP, name: 'Pickup Chip' },
};

const CHIP_LOOKUP = {
  id: '637574',
  workflowData: {
    id: WF_CHIP,
    name: 'Pickup Chip',
    nodes: [planeNode('Chip — List Labels')],
  },
  data: {
    resultData: {
      runData: {
        'Chip — Target': runItem(
          { ws: '33god', board: HEYMA_BOARD, ticketId: HEYMA_TICKET, ticketKey: 'HEYMA-1', agentId: 'heyma-pm' },
          { ws: 'automaticai', board: GRUV_BOARD, ticketId: 'c0ffee00-0000-4000-8000-000000000147', ticketKey: 'GRUV-147', agentId: 'gruv-pm' },
          { ws: 'automaticai', board: JIMB_BOARD, ticketId: JIMB_TICKET, ticketKey: 'JIMB-3', agentId: 'james-brennan-pm' },
        ),
      },
    },
  },
};

const TRIGGER_ONLY_ERROR = {
  trigger: { error: { message: 'NatsError: CONNECTION_REFUSED 127.0.0.1:4222' }, mode: 'trigger' },
  workflow: { id: WF_DELEGATION, name: 'Ticket Delegation' },
};

function envelopeFor(report) {
  const options = {
    type: TYPE,
    kind: 'event',
    validate: true,
    data: report.data,
    eventId: report.eventId,
    msgId: report.msgId,
    correlationId: report.correlationId,
    causationId: report.causationId,
    observedAt: report.observedAt,
    orderingKey: report.orderingKey,
    source: 'urn:33god:integration:n8n:agent-fleet',
    producer: 'n8n',
    service: 'n8n-lifecycle-lane-errors',
  };
  return buildEnvelope(options).envelope;
}

/** Report, then hold the finished envelope to its canonical schema. */
function reportValid(input) {
  const report = reportLaneFailure({ registry: REGISTRY, now: new Date('2026-11-05T20:00:00Z'), ...input });
  validateEnvelope(TYPE, envelopeFor(report));
  return report;
}

const classOf = (error) =>
  reportValid({
    errorPayload: { execution: { id: '1', error }, workflow: { id: WF_GROOMING, name: 'Ticket Grooming' } },
    lookup: { error: { message: 'lookup failed' } },
  }).data.error;

// ---------------------------------------------------------------------------
// Per-lane extraction
// ---------------------------------------------------------------------------

test('grooming: a Fleet throw reports the ticket at data.ticket_key and retries only (Backlog-born)', () => {
  const report = reportValid({ errorPayload: GROOMING_ERROR, lookup: GROOMING_LOOKUP });
  const { data } = report;
  assert.equal(data.workflow, 'ticket-grooming');
  assert.equal(data.run_id, '640001');
  assert.equal(data.stage, 'Groom Ticket');
  assert.equal(data.lane.engine, 'n8n');
  assert.equal(data.lane.workflow_id, WF_GROOMING);
  assert.equal(data.lane.execution_id, '640001');
  assert.equal(data.lane.trigger_type, 'bloodbank.repo.task.created');
  assert.equal(data.lane.trigger_event_id, TRIGGER_EVENT);
  assert.equal(data.lane.mode, 'trigger');
  assert.equal(data.lane.retry_of, null);
  assert.equal(data.context.ticket_key, 'JIMB-9');
  assert.equal(data.context.ticket_id, JIMB_TICKET);
  assert.equal(data.context.board_id, JIMB_BOARD);
  assert.equal(data.context.reason, 'ticket-grooming');
  assert.equal(data.context.phase, 'Backlog');
  assert.equal(data.error.class, 'node_error');
  // The owner resolves by board from the registry; not enrolled, so no identity.
  assert.equal(data.target_agent_id, 'james-brennan-pm');
  assert.equal(data.matched_by, 'board');
  assert.equal(data.company_identity, null);
  assert.equal(data.gateway_consumer, 'hermes');
  // A Backlog-born ticket is retried, never bounced: bouncing promotes it.
  assert.equal(data.recovery.action, 'retry_execution');
  assert.match(data.recovery.hint, /api\/v1\/executions\/640001\/retry/);
  assert.match(data.recovery.hint, /do NOT bounce/);
  assert.doesNotMatch(data.recovery.hint, /px move/);
  assert.equal(data.severity, 'page');
  assert.equal(data.redacted, true);
  assert.equal(report.correlationId, TRIGGER_CORRELATION);
  assert.equal(report.causationId, TRIGGER_EVENT);
  assert.equal(report.orderingKey, `task:james-brennan:${JIMB_TICKET}`);
  assert.deepEqual(data.links.map((link) => link.rel), ['page', 'run']);
  assert.equal(data.links[0].url, 'https://plane.delo.sh/automaticai/browse/JIMB-9/');
});

test('grooming of a Todo-born ticket may be retried or bounced', () => {
  const lookup = structuredClone(GROOMING_LOOKUP);
  lookup.data.resultData.runData.onTicketCreated = runItem(createdEnvelope({ phase: 'Todo' }));
  const { data } = reportValid({ errorPayload: GROOMING_ERROR, lookup });
  assert.equal(data.recovery.action, 'retry_or_requeue');
  assert.match(data.recovery.hint, /px move JIMB-9 Backlog -m "lane failed \(execution 640001\)" && px move JIMB-9 Todo/);
});

test('delegation: the transition trigger carries the ticket; a state UUID is the previous phase', () => {
  const lookup = {
    id: '640002',
    data: {
      resultData: {
        runData: {
          onTicketTransitioned: runItem({
            ...createdEnvelope({ phase: 'Todo', previous_phase: 'd1b2c3d4-0000-4000-8000-00000000abcd' }),
            type: 'bloodbank.repo.task.updated',
          }),
        },
      },
    },
  };
  const { data } = reportValid({
    errorPayload: {
      execution: { id: '640002', lastNodeExecuted: 'Delegate Ticket', error: { message: 'boom' } },
      workflow: { id: WF_DELEGATION, name: 'Ticket Delegation' },
    },
    lookup,
  });
  assert.equal(data.workflow, 'ticket-delegation');
  assert.equal(data.context.reason, 'ticket-delegation');
  assert.equal(data.context.previous_phase, 'd1b2c3d4-0000-4000-8000-00000000abcd');
  assert.equal(data.lane.trigger_type, 'bloodbank.repo.task.updated');
  assert.equal(data.recovery.action, 'retry_or_requeue');
});

test('groomed: the HEYMA-12 429 shape names the owner, the identity and the shared Plane key', () => {
  const report = reportValid({ errorPayload: GROOMED_ERROR, lookup: GROOMED_LOOKUP });
  const { data } = report;
  assert.equal(data.workflow, 'ticket-delegation-groomed');
  assert.equal(data.run_id, '632616');
  assert.equal(data.stage, 'Groomed in Todo?');
  assert.equal(data.context.ticket_key, 'HEYMA-12');
  assert.equal(data.context.reason, 'ticket-delegation');
  assert.equal(data.context.workspace, '33god');
  assert.equal(data.target_agent_id, 'heyma-pm');
  assert.deepEqual(data.company_identity, {
    subject: SUBJECT,
    handle: 'grolf',
    email: 'grolf@agents.delo.sh',
    status: 'active',
    plane_attribution: 'native',
  });
  assert.equal(data.gateway_consumer, 'hermes-33god-pm');
  assert.deepEqual(data.error, {
    class: 'rate_limited',
    http_status: 429,
    upstream: 'plane',
    credential_scope: 'fleet_shared',
    retry_after_seconds: null,
  });
  assert.equal(data.recovery.action, 'retry_or_requeue');
  assert.match(data.recovery.hint, /px move HEYMA-12 Backlog -m "lane failed \(execution 632616\)" && px move HEYMA-12 Todo/);
  assert.match(data.recovery.hint, /one ticket at a time on WIP=1/);
  assert.ok(data.recovery.hint.length <= 400);
  assert.equal(data.severity, 'page');
  assert.equal(report.orderingKey, `task:transcription-queue:${HEYMA_TICKET}`);
  assert.equal(report.observedAt, '2026-11-05T19:04:42.255Z');
  assert.equal(data.links[0].url, 'https://plane.delo.sh/33god/browse/HEYMA-12/');
  assert.ok(data.summary.length <= 200);
  assert.match(data.summary, /HEYMA-12/);
});

test('chip: the failing ticket comes from stack line 1, never from the 3-ticket sweep order', () => {
  const report = reportValid({ errorPayload: CHIP_ERROR, lookup: CHIP_LOOKUP });
  const { data } = report;
  assert.equal(data.workflow, 'ticket-pickup-chip');
  assert.equal(data.context.ticket_key, 'GRUV-147');
  assert.equal(data.context.board_id, GRUV_BOARD);
  assert.equal(data.context.ticket_id, 'c0ffee00-0000-4000-8000-000000000147');
  assert.equal(data.context.workspace, 'automaticai');
  assert.equal(data.context.reason, 'ticket-pickup-chip');
  assert.equal(data.target_agent_id, 'gruv-pm');
  // gruv-pm is not in the registry: the owner is named but has no identity.
  assert.equal(data.company_identity, null);
  assert.equal(data.gateway_consumer, null);
  assert.equal(data.error.class, 'upstream_unavailable');
  assert.equal(data.error.http_status, 500);
  assert.equal(data.error.upstream, 'plane');
  assert.equal(data.severity, 'low');
  assert.equal(data.recovery.action, 'none');
  assert.match(data.recovery.hint, /sweep heals it/);
  assert.equal(data.lane.trigger_type, null);
});

test('chip: a stack that names no ticket reports none rather than guessing from the sweep', () => {
  const error = structuredClone(CHIP_ERROR);
  error.execution.error.stack = 'Error: something else\n    at x';
  const { data } = reportValid({ errorPayload: error, lookup: CHIP_LOOKUP });
  assert.equal(data.context.ticket_key, null);
  assert.equal(data.severity, 'low');
});

test('a failure inside the trigger itself still reports, with a stable synthetic run id', () => {
  const at = (iso) =>
    reportValid({
      errorPayload: TRIGGER_ONLY_ERROR,
      lookup: { error: { message: 'Could not get parameter' } },
      now: new Date(iso),
    });
  const first = at('2026-11-05T20:01:00Z');
  const sameHour = at('2026-11-05T20:42:00Z');
  const nextHour = at('2026-11-05T21:01:00Z');
  assert.match(first.data.run_id, /^trigger-[0-9a-f]{12}$/);
  assert.equal(first.data.stage, 'trigger');
  assert.equal(first.data.workflow, 'ticket-delegation');
  assert.equal(first.data.error.class, 'bus_unavailable');
  assert.equal(first.data.error.upstream, 'nats');
  assert.equal(first.data.error.credential_scope, 'none');
  assert.equal(first.data.context.ticket_key, null);
  assert.equal(first.data.recovery.action, 'none');
  assert.equal(first.orderingKey, `workflow:${WF_DELEGATION}:${first.data.run_id}`);
  assert.equal(first.eventId, sameHour.eventId, 'a trigger failing all hour is one fact');
  assert.notEqual(first.eventId, nextHour.eventId);
  assert.equal(first.data.lane.mode, 'trigger');
  assert.equal(first.data.lane.execution_url, null);
  assert.equal(first.data.links, undefined);
});

test('a failed lookup still reports: the ticket comes from the error stack, the owner from the registry', () => {
  const { data } = reportValid({ errorPayload: GROOMED_ERROR, lookup: { error: { message: 'ECONNREFUSED' } } });
  assert.equal(data.context.ticket_key, 'HEYMA-12');
  assert.equal(data.context.board_id, HEYMA_BOARD);
  assert.equal(data.context.ticket_id, null);
  // The workspace is not in the run data, so it falls back to the owner's row.
  assert.equal(data.context.workspace, '33god');
  assert.equal(data.target_agent_id, 'heyma-pm');
  assert.equal(data.matched_by, 'board');
  assert.equal(data.company_identity.handle, 'grolf');
  assert.equal(data.error.class, 'rate_limited');
  assert.equal(data.lane.trigger_event_id, null);
  assert.equal(data.recovery.action, 'retry_or_requeue');
});

test('an unknown workflow reports as its slugified name and takes the first ticket it finds', () => {
  const { data } = reportValid({
    errorPayload: {
      execution: { id: '9', lastNodeExecuted: 'Step', error: { message: 'oops' } },
      workflow: { id: 'zzzzzzzzzzzzzzzz', name: 'My New Flow — v2' },
    },
    lookup: {
      id: '9',
      data: { resultData: { runData: { Pick: runItem({ ticketKey: 'ABC-1', ws: 'ws1' }) } } },
    },
  });
  assert.equal(data.workflow, 'my-new-flow-v2');
  assert.equal(data.context.reason, 'my-new-flow-v2');
  assert.equal(data.context.ticket_key, 'ABC-1');
  assert.equal(data.recovery.action, 'retry_execution');
});

test('the lane map is configurable and falls back to the four lanes', () => {
  assert.deepEqual(parseLaneMap(undefined), { ...DEFAULT_LANE_MAP });
  assert.deepEqual(parseLaneMap('not json'), { ...DEFAULT_LANE_MAP });
  assert.deepEqual(parseLaneMap('{"abc":"my-lane"}'), { abc: 'my-lane' });
  assert.deepEqual(Object.values(DEFAULT_LANE_MAP).sort(), [
    'ticket-delegation',
    'ticket-delegation-groomed',
    'ticket-grooming',
    'ticket-pickup-chip',
  ]);
  const { data } = reportValid({
    errorPayload: GROOMING_ERROR,
    lookup: GROOMING_LOOKUP,
    laneMap: JSON.stringify({ [WF_GROOMING]: 'ticket-grooming' }),
  });
  assert.equal(data.workflow, 'ticket-grooming');
  assert.equal(slugify('Ticket Delegation — Groomed in Todo'), 'ticket-delegation-groomed-in-todo');
  assert.equal(slugify('   '), 'unknown-workflow');
});

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

test('error classification table', () => {
  const cases = [
    [{ message: 'x', httpCode: '429' }, 'rate_limited', 429],
    [{ message: 'Request failed with status code 429' }, 'rate_limited', 429],
    [{ message: 'The service is receiving too many requests from you' }, 'rate_limited', 429],
    [{ message: 'Plane read failed: HTTP 401' }, 'auth_failed', 401],
    [{ message: 'x', httpCode: 403 }, 'auth_failed', 403],
    [{ message: 'Forbidden - perhaps check your credentials' }, 'auth_failed', 403],
    [{ message: 'x', httpCode: '404' }, 'not_found', 404],
    [{ message: 'HTTP 410 gone' }, 'not_found', 410],
    [{ message: 'HTTP 422 Unprocessable' }, 'invalid_payload', 422],
    [{ message: 'HTTP 418 teapot' }, 'node_error', 418],
    [{ message: 'HTTP 500 500 - "{\\"error\\"' }, 'upstream_unavailable', 500],
    [{ message: 'Service unavailable - try again later' }, 'upstream_unavailable', 503],
    [{ message: 'x', httpCode: 502 }, 'upstream_unavailable', 502],
    [{ message: 'connect ECONNREFUSED 127.0.0.1:5678' }, 'upstream_unavailable', null],
    [{ message: 'getaddrinfo ENOTFOUND plane.delo.sh' }, 'upstream_unavailable', null],
    [{ message: 'connect ETIMEDOUT 10.0.0.1:443' }, 'timeout', null],
    [{ message: 'The request timed out' }, 'timeout', null],
    [{ message: 'x', httpCode: 408 }, 'timeout', 408],
    [{ message: 'NatsError: CONNECTION_REFUSED' }, 'bus_unavailable', null],
    [{ message: 'JetStream publish failed' }, 'bus_unavailable', null],
    [{ message: 'no registry entry matches board abc and no fallback agent x-pm exists' }, 'no_route', null],
    [{ message: 'heyma-pm is registry-defined but bloodbank.enabled is false' }, 'ineligible', null],
    [{ message: 'the item carries neither data.repo nor a board id, so no fleet agent can be resolved' }, 'invalid_payload', null],
    [{ message: 'Cannot read the fleet registry at /x: ENOENT' }, 'node_error', null],
    [{}, 'unknown', null],
  ];
  for (const [error, klass, status] of cases) {
    const got = classOf(error);
    assert.equal(got.class, klass, JSON.stringify(error));
    assert.equal(got.http_status, status, JSON.stringify(error));
  }
});

test('Retry-After is parsed from the message or the response headers', () => {
  assert.equal(classOf({ message: 'HTTP 429 Retry-After: 30' }).retry_after_seconds, 30);
  assert.equal(
    classOf({ message: 'HTTP 429', cause: { response: { headers: { 'retry-after': '12' } } } }).retry_after_seconds,
    12,
  );
  assert.equal(classOf({ message: 'HTTP 429' }).retry_after_seconds, null);
  const { data } = reportValid({
    errorPayload: { execution: { id: '1', error: { message: 'HTTP 429 retry after 45 seconds' } }, workflow: { id: WF_GROOMING } },
    lookup: {},
  });
  assert.match(data.recovery.hint, /Wait 45s first/);
});

test('a Plane HTTP failure is the shared fleet credential; a rejected key says to rotate it', () => {
  const lookup = structuredClone(GROOMED_LOOKUP);
  const error = structuredClone(GROOMED_ERROR);
  error.execution.error.message = 'HTTP 403 Forbidden';
  error.execution.error.stack = error.execution.error.stack.replace('HTTP 429', 'HTTP 403');
  const { data } = reportValid({ errorPayload: error, lookup });
  assert.equal(data.error.class, 'auth_failed');
  assert.equal(data.error.credential_scope, 'fleet_shared');
  assert.equal(data.recovery.action, 'rotate_credential');
  assert.match(data.recovery.hint, /TpxVVgOnmjHaE6h7/);
});

test('auth_failed with an employee credential rotates it; unenrolled owner is enroll_identity', () => {
  const asEmployee = (agentId) => {
    const lookup = structuredClone(GROOMED_LOOKUP);
    lookup.data.resultData.runData.onGroomingCompleted[0].data.main[0][0].json.data.target_agent_id = agentId;
    return reportValid({
      errorPayload: { ...GROOMED_ERROR, credential_scope: 'employee', execution: { ...GROOMED_ERROR.execution, error: { message: 'HTTP 401 Unauthorized' } } },
      lookup,
    }).data;
  };
  const enrolled = asEmployee('heyma-pm');
  assert.equal(enrolled.error.credential_scope, 'employee');
  assert.equal(enrolled.recovery.action, 'rotate_credential');
  assert.match(enrolled.recovery.hint, /grolf/);
  const unenrolled = asEmployee('james-brennan-pm');
  assert.equal(unenrolled.company_identity, null);
  assert.equal(unenrolled.recovery.action, 'enroll_identity');
});

test('no_route and ineligible are fix_route; an unenrolled owner alone leaves recovery lane-driven', () => {
  const fix = classOf({ message: 'no registry entry matches board abc and no fallback agent x-pm exists' });
  assert.equal(fix.class, 'no_route');
  const { data } = reportValid({
    errorPayload: {
      execution: { id: '5', lastNodeExecuted: 'Delegate Ticket', error: { message: 'heyma-pm is registry-defined but bloodbank.enabled is false' } },
      workflow: { id: WF_DELEGATION, name: 'Ticket Delegation' },
    },
    lookup: GROOMING_LOOKUP,
  });
  assert.equal(data.error.class, 'ineligible');
  assert.equal(data.recovery.action, 'fix_route');
  const lane = reportValid({ errorPayload: GROOMING_ERROR, lookup: GROOMING_LOOKUP }).data;
  assert.equal(lane.company_identity, null);
  assert.equal(lane.recovery.action, 'retry_execution');
});

// ---------------------------------------------------------------------------
// Owner and identity
// ---------------------------------------------------------------------------

test('company identity is projected only when the whole block is valid', () => {
  const good = REGISTRY.agents['heyma-pm'];
  assert.equal(companyIdentityOf(good).plane_attribution, 'native');
  const unbound = structuredClone(good);
  delete unbound.company_identity.plane;
  assert.equal(companyIdentityOf(unbound).plane_attribution, 'unbound');
  for (const mutate of [
    (id) => { id.subject = 'not-a-uuid'; },
    (id) => { id.email = 'grolf@example.com'; },
    (id) => { id.status = 'fired'; },
    (id) => { id.handle = ''; },
  ]) {
    const bad = structuredClone(good);
    mutate(bad.company_identity);
    assert.equal(companyIdentityOf(bad), null);
  }
  assert.equal(companyIdentityOf(REGISTRY.agents['james-brennan-pm']), null);
  assert.equal(companyIdentityOf(undefined), null);
  // A malformed block degrades to "not enrolled"; the envelope stays valid.
  const registry = structuredClone(REGISTRY);
  registry.agents['heyma-pm'].company_identity.email = 'nope';
  const { data } = reportValid({ errorPayload: GROOMED_ERROR, lookup: GROOMED_LOOKUP, registry });
  assert.equal(data.company_identity, null);
});

test('a missing or unreadable registry leaves the owner unresolved but the report intact', () => {
  const { data } = reportValid({ errorPayload: GROOMING_ERROR, lookup: GROOMING_LOOKUP, registry: undefined });
  assert.equal(data.target_agent_id, null);
  assert.equal(data.matched_by, null);
  assert.equal(data.company_identity, null);
  assert.equal(data.gateway_consumer, null);
  assert.equal(data.context.ticket_key, 'JIMB-9');
  // A board nobody owns is reported as no_route's "none", with no target.
  const registry = { schema_version: 1, agents: {} };
  const none = reportValid({ errorPayload: GROOMING_ERROR, lookup: GROOMING_LOOKUP, registry }).data;
  assert.equal(none.target_agent_id, null);
  assert.equal(none.matched_by, 'none');
});

test('no credential reference ever reaches the report', () => {
  const registry = structuredClone(REGISTRY);
  registry.agents['heyma-pm'].plane.key_ref = 'op://DeLoSecrets/abc123/credential';
  registry.agents['heyma-pm'].company_identity.credential_ref = 'op://DeLoSecrets/def456/credential';
  const text = JSON.stringify(reportValid({ errorPayload: GROOMED_ERROR, lookup: GROOMED_LOOKUP, registry }));
  assert.doesNotMatch(text, /op:\/\//);
  assert.doesNotMatch(text, /key_ref|credential_ref/);
});

// ---------------------------------------------------------------------------
// Ids, redaction
// ---------------------------------------------------------------------------

test('ids are a pure function of the failed run', () => {
  const a = reportValid({ errorPayload: GROOMED_ERROR, lookup: GROOMED_LOOKUP });
  const b = reportValid({ errorPayload: GROOMED_ERROR, lookup: { error: { message: 'x' } } });
  const other = reportValid({
    errorPayload: { ...GROOMED_ERROR, execution: { ...GROOMED_ERROR.execution, id: '632617' } },
    lookup: GROOMED_LOOKUP,
  });
  const { deterministicUuid } = require('../src/index.ts');
  assert.equal(a.eventId, deterministicUuid(`workflow.task.failed:${WF_GROOMED}:632616`));
  assert.equal(a.msgId, a.eventId);
  assert.equal(a.eventId, b.eventId, 'the lookup does not move the id');
  assert.notEqual(a.eventId, other.eventId);
  assert.equal(envelopeFor(a).id, a.eventId);
});

test('anything secret-shaped is stripped from the reason and everywhere else', () => {
  const secretLike = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0';
  const message = [
    'HTTP 401 Bearer abcdef1234567890XYZ rejected',
    'X-Api-Key: plane_api_0123456789abcdef',
    'ref op://DeLoSecrets/Plane/credential',
    `token ${secretLike}`,
    'url https://user:hunter2@plane.delo.sh/x?api_key=zzz999',
  ].join(' ');
  const report = reportValid({
    errorPayload: { execution: { id: '7', error: { message, description: message, stack: message } }, workflow: { id: WF_GROOMING } },
    lookup: {},
  });
  const text = JSON.stringify(report);
  for (const leak of ['abcdef1234567890XYZ', 'plane_api_0123456789abcdef', 'op://DeLoSecrets', secretLike, 'hunter2', 'zzz999']) {
    assert.ok(!text.includes(leak), `${leak} leaked`);
  }
  assert.equal(report.data.redacted, true);
  assert.ok(report.data.reason.length <= 500);
  assert.equal(redactText(HEYMA_BOARD), HEYMA_BOARD, 'a UUID is an id, not a secret');
  assert.equal(redactText('ticket-delegation-groomed-in-todo'), 'ticket-delegation-groomed-in-todo');
  const long = reportValid({
    errorPayload: { execution: { id: '8', error: { message: 'x'.repeat(2000) } }, workflow: { id: WF_GROOMING } },
    lookup: {},
  });
  assert.ok(long.data.reason.length <= 500);
  assert.ok(!JSON.stringify(long).includes('at /home/'), 'the stack is never copied');
});

test('the stack is read for the ticket but never copied onto the bus', () => {
  const text = JSON.stringify(reportValid({ errorPayload: GROOMING_ERROR, lookup: GROOMING_LOOKUP }));
  assert.doesNotMatch(text, /\/home\/someone/);
  assert.doesNotMatch(text, /"stack"/);
});

// ---------------------------------------------------------------------------
// Never throws
// ---------------------------------------------------------------------------

test('garbage in, a valid report out', () => {
  const circular = {};
  circular.self = circular;
  const garbage = [
    undefined,
    null,
    42,
    'not json',
    '{"execution": "nope"}',
    [],
    [1, 2, 3],
    circular,
    { execution: null, workflow: null },
    { execution: { id: { nested: true }, error: 'a string error', lastNodeExecuted: 7 }, workflow: [] },
    { execution: { error: { message: { weird: true }, stack: 12, httpCode: 'abc' } } },
    { trigger: 'x', workflow: { id: 5, name: false } },
    { execution: { id: '1', url: 'javascript:alert(1)', error: { message: 'x' } }, workflow: { id: WF_CHIP } },
  ];
  for (const errorPayload of garbage) {
    for (const lookup of [...garbage, { data: 'flatted-string' }, { data: { resultData: { runData: { onGroomingCompleted: 'x' } } } }]) {
      for (const registry of [undefined, null, 'x', { agents: 'x' }, { agents: { 'heyma-pm': 'x' } }]) {
        const report = reportLaneFailure({ errorPayload, lookup, registry, laneMap: errorPayload });
        validateEnvelope(TYPE, envelopeFor(report));
        assert.ok(report.page.title && report.page.message && report.page.click);
      }
    }
  }
  validateEnvelope(TYPE, envelopeFor(reportLaneFailure({})));
});

// ---------------------------------------------------------------------------
// The node
// ---------------------------------------------------------------------------

async function tempRegistry(t, value) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.lane-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'agents-registry.yaml');
  await writeFile(file, stringifyYaml(value), 'utf8');
  return file;
}

function context(parameters, input) {
  return {
    getInputData: () => input,
    getNodeParameter(name, _index, fallback) {
      const value = Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
      if (value instanceof Error) throw value;
      return value;
    },
    getNode: () => ({ id: 'n', name: 'Report Lane Failure', type: 'n8n-nodes-bloodbank.bloodbankFleet', typeVersion: 1, position: [0, 0], parameters: {} }),
    continueOnFail: () => false,
  };
}

function recorder({ fail } = {}) {
  const calls = [];
  return {
    calls,
    async send(options) {
      calls.push(options);
      if (fail) throw new Error(fail);
      return { subject: 'bloodbank.evt.workflow.task.failed', correlationid: options.correlationId, eventId: options.eventId };
    },
  };
}

const capturing = (messages) => (options) =>
  publish(options, async () => ({
    publish(subject, data, opts) {
      messages.push({ subject, envelope: JSON.parse(Buffer.from(data).toString('utf8')), headers: opts?.headers });
    },
    flush: async () => {},
    drain: async () => {},
  }));

async function runNode(t, parameters, input, send) {
  const registryFile = await tempRegistry(t, REGISTRY);
  return Fleet.prototype.execute.call(context({ operation: 'reportLaneFailure', registryFile, ...parameters }, input), send);
}

test('the node declares Report Lane Failure with the documented parameters and defaults', () => {
  const { description } = new Fleet();
  const operation = description.properties.find((property) => property.name === 'operation');
  assert.ok(operation.options.some((option) => option.value === 'reportLaneFailure' && option.name === 'Report Lane Failure'));
  const shown = (name) => description.properties.filter((property) => property.name === name);
  const [errorPayload] = shown('errorPayload');
  assert.equal(errorPayload.type, 'json');
  // No default: n8n drops a parameter equal to its default from the saved
  // workflow, so a defaulted Error Payload would leave the export silent about
  // where the Error Trigger item comes from. The wiring must say it.
  assert.equal(errorPayload.default, '');
  assert.equal(errorPayload.placeholder, "={{ $('Lane Failed').first().json }}");
  assert.deepEqual(errorPayload.displayOptions, { show: { operation: ['reportLaneFailure'] } });
  const [laneMap] = shown('laneMap');
  assert.equal(laneMap.type, 'json');
  assert.deepEqual(JSON.parse(laneMap.default), { ...DEFAULT_LANE_MAP });
  const services = shown('service');
  assert.equal(services.length, 2);
  assert.equal(services.find((property) => property.displayOptions.show).default, 'n8n-lifecycle-lane-errors');
  assert.equal(services.find((property) => property.displayOptions.hide).default, 'n8n-agent-fleet');
  // The old operations are untouched.
  assert.deepEqual(operation.options.map((option) => option.value).slice(0, 2), ['groomTicket', 'delegateTicket']);
});

test('the node publishes the fact and puts event and page on every Published item', async (t) => {
  const rec = recorder();
  const [published, notPublished] = await runNode(
    t,
    { errorPayload: GROOMED_ERROR },
    [{ json: GROOMED_LOOKUP }],
    rec.send,
  );
  assert.equal(notPublished.length, 0);
  assert.equal(published.length, 1);
  const call = rec.calls[0];
  assert.equal(call.type, TYPE);
  assert.equal(call.kind, 'event');
  assert.equal(call.validate, true);
  assert.equal(call.service, 'n8n-lifecycle-lane-errors');
  assert.equal(call.source, 'urn:33god:integration:n8n:agent-fleet');
  assert.equal(call.producer, 'n8n');
  assert.equal(call.msgId, call.eventId);
  assert.equal(call.correlationId, TRIGGER_CORRELATION);
  assert.equal(call.causationId, TRIGGER_EVENT);
  assert.equal(call.orderingKey, `task:transcription-queue:${HEYMA_TICKET}`);
  const item = published[0].json;
  assert.equal(item.published, true);
  assert.equal(item.event.type, TYPE);
  assert.equal(item.event.data.workflow, 'ticket-delegation-groomed');
  assert.equal(item.page.title, 'Lane failed: HEYMA-12');
  assert.match(item.page.message, /Owner: heyma-pm \(grolf, grolf@agents\.delo\.sh, active\)/);
  assert.match(item.page.message, /failed at Groomed in Todo\?/);
  assert.match(item.page.message, /Recovery: .*px move HEYMA-12/);
  assert.equal(item.page.click, 'https://plane.delo.sh/33god/browse/HEYMA-12/');
  assert.equal(item.page.priority, '4');
  assert.match(item.page.tags, /lane-error/);
  assert.deepEqual(published[0].pairedItem, { item: 0 });
});

test('the real publisher validates the fact and stamps Nats-Msg-Id', async (t) => {
  const messages = [];
  const [published, notPublished] = await runNode(
    t,
    { errorPayload: GROOMED_ERROR },
    [{ json: GROOMED_LOOKUP }],
    capturing(messages),
  );
  assert.equal(notPublished.length, 0);
  assert.equal(published.length, 1);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].subject, 'bloodbank.evt.workflow.task.failed');
  validateEnvelope(TYPE, messages[0].envelope);
  assert.equal(messages[0].envelope.ordering_key, `task:transcription-queue:${HEYMA_TICKET}`);
  assert.equal(messages[0].envelope.producer, 'n8n');
  assert.equal(messages[0].headers.get('Nats-Msg-Id'), messages[0].envelope.id);
  assert.equal(published[0].json.event.id, messages[0].envelope.id);
});

test('a bus failure goes to Not Published with the page and the error', async (t) => {
  const rec = recorder({ fail: 'NATS connection refused' });
  const [published, notPublished] = await runNode(
    t,
    { errorPayload: GROOMED_ERROR },
    [{ json: GROOMED_LOOKUP }],
    rec.send,
  );
  assert.equal(published.length, 0);
  assert.equal(notPublished.length, 1);
  const item = notPublished[0].json;
  assert.equal(item.published, false);
  assert.equal(item.publishError, 'NATS connection refused');
  assert.equal(item.event.data.workflow, 'ticket-delegation-groomed');
  assert.equal(item.page.title, 'Lane failed: HEYMA-12');
});

test('a schema-invalid fact is Not Published, with the validation error, through the real publisher', async (t) => {
  const real = capturing([]);
  // Corrupt the finished data on its way to the real publisher: its own
  // validation refuses it before any connection is used.
  const send = (options) => real({ ...options, data: { ...options.data, workflow: '' } });
  const [published, notPublished] = await runNode(t, { errorPayload: GROOMED_ERROR }, [{ json: GROOMED_LOOKUP }], send);
  assert.equal(published.length, 0);
  assert.equal(notPublished.length, 1);
  assert.match(notPublished[0].json.publishError, /schema validation failed/);
  assert.ok(notPublished[0].json.page.message);
});

test('the node never throws: malformed items, a failing parameter expression, an unreadable registry', async (t) => {
  const rec = recorder();
  const input = [{ json: undefined }, { json: 'x' }, { json: { error: { message: 'ECONNREFUSED' } } }, { json: GROOMING_LOOKUP }];
  const [published, notPublished] = await runNode(
    t,
    { errorPayload: new Error('Referenced node is unexecuted'), registryFile: '/nonexistent/registry.yaml' },
    input,
    rec.send,
  );
  assert.equal(published.length, 4);
  assert.equal(notPublished.length, 0);
  for (const item of published) {
    assert.ok(item.json.event);
    assert.ok(item.json.page.message);
  }
});

test('it also lands the item when nothing at all is wired', async (t) => {
  const rec = recorder();
  const [published] = await runNode(t, {}, [{ json: {} }], rec.send);
  assert.equal(published.length, 1);
  assert.equal(published[0].json.event.data.workflow, 'unknown-workflow');
});

// ---------------------------------------------------------------------------
// Groom/delegate commands carry no registry-derived identity stamp
// ---------------------------------------------------------------------------

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys(value[key])]));
  }
  return value;
}

/** The Hermes gateway's journal digest: sha256 of the sorted, compact envelope
 *  (bloodbank_hermes_gateway/execution_state.py envelope_digest). */
const gatewayDigest = (envelope) => createHash('sha256').update(JSON.stringify(sortKeys(envelope))).digest('hex');

test('a redelivered groom/delegate command is byte-identical whatever the registry now says about the owner identity', async (t) => {
  // The gateway journals command_id + that digest and terminally rejects
  // (TERM, no event) a same-id redelivery with another digest. A stamp read
  // from the registry at dispatch time would turn an enrollment, a suspension
  // or a handle change between delivery and redelivery into exactly that.
  const variant = (mutate) => {
    const registry = structuredClone(REGISTRY);
    mutate(registry.agents['heyma-pm']);
    return registry;
  };
  const registries = {
    enrolled: REGISTRY,
    unenrolled: variant((row) => { delete row.company_identity; }),
    suspended: variant((row) => { row.company_identity.status = 'suspended'; }),
    retired: variant((row) => { row.company_identity.status = 'retired'; }),
    renamed: variant((row) => { row.company_identity.handle = 'grolf-2'; }),
  };
  const causing = {
    id: TRIGGER_EVENT,
    time: '2026-11-05T19:00:00.000Z',
    type: 'bloodbank.repo.task.created',
    data: {
      repo: 'transcription-queue', board_id: HEYMA_BOARD, ticket_key: 'HEYMA-12', ticket_id: HEYMA_TICKET,
      workspace: '33god', phase: 'Todo',
    },
  };
  for (const operation of ['groomTicket', 'delegateTicket']) {
    const digests = new Set();
    for (const [label, registry] of Object.entries(registries)) {
      const registryFile = await tempRegistry(t, registry);
      const calls = [];
      const send = async (options) => {
        calls.push(options);
        return { subject: 's', correlationid: 'c', eventId: 'e', commandId: options.commandId };
      };
      await Fleet.prototype.execute.call(context({ operation, registryFile }, [{ json: causing }]), send);
      const command = calls.find((call) => call.type === COMMAND);
      assert.ok(command, `${operation}/${label} dispatched`);
      assert.equal('employee' in command.data.context, false, `${operation}/${label} stamps no identity`);
      const envelope = buildEnvelope(command).envelope;
      validateEnvelope(COMMAND, envelope);
      digests.add(gatewayDigest(envelope));
    }
    assert.equal(digests.size, 1, `${operation}: one gateway digest across every registry state`);
  }
});

// ---------------------------------------------------------------------------
// Adversarial review of 0.8.0
// ---------------------------------------------------------------------------

const NOW = new Date('2026-11-05T20:00:00Z');
const failing = (error, extra = {}) =>
  reportValid({
    errorPayload: { execution: { id: '21', error, ...extra }, workflow: { id: WF_GROOMING, name: 'Ticket Grooming' } },
    lookup: {},
    now: NOW,
  });

test('redaction runs once: an op:// ref keeps one bracket and the summary stays within 200', () => {
  const ref = 'op://DeLoSecrets/Plane/credential';
  // The ref sits inside the first 199 characters of a summary that is capped.
  const report = failing({ message: `boom ${ref}` }, { lastNodeExecuted: `${'N'.repeat(100)} ${ref} ${'M'.repeat(100)}` });
  assert.equal(report.data.reason, 'boom op://[redacted]');
  assert.ok(report.data.summary.length <= 200, `summary is ${report.data.summary.length} long`);
  assert.match(report.data.summary, /op:\/\/\[redacted\] /);
  const text = JSON.stringify(report);
  assert.doesNotMatch(text, /\[redacted\]\]/);
  assert.doesNotMatch(report.page.message, /\[redacted\]\]/);
  assert.match(report.page.message, /boom op:\/\/\[redacted\]$/m);
});

test('redactText is idempotent', () => {
  const samples = [
    'op://DeLoSecrets/x/credential',
    'Authorization: Bearer abcdef1234567890',
    'Bearer abcdef1234567890',
    'Basic dXNlcjpwYXNzd29yZA==',
    'token=abc123 refresh_token=def456',
    'https://u:p@plane.delo.sh/x?api_key=zzz',
    'password hunter2',
    'session=s3ss10n',
  ];
  for (const sample of samples) {
    const once = redactText(sample);
    assert.equal(redactText(once), once, sample);
  }
});

test('the 32-character backstop leaves slug-shaped fields alone', () => {
  const name = 'ticket-delegation-groomed-2026-q4-rollout-v2';
  const stage = 'delegate-groomed-ticket-2026-11-05-retry-v2';
  const report = reportValid({
    errorPayload: {
      execution: { id: '22', lastNodeExecuted: stage, error: { message: 'oops' } },
      workflow: { id: 'zzzzzzzzzzzzzzzz', name },
    },
    lookup: {},
    now: NOW,
  });
  const { data } = report;
  assert.equal(data.workflow, name, 'an unmapped workflow keeps a useful slug');
  assert.equal(data.lane.workflow_name, name);
  assert.equal(data.stage, stage);
  assert.equal(data.context.reason, name);
  assert.equal(data.summary, `${name}: ${name} failed at ${stage} (node_error)`);
  assert.match(report.page.tags, new RegExp(`,${name}$`));
  // Explicit credential syntax is still stripped from a slug-shaped field.
  const keyed = reportValid({
    errorPayload: {
      execution: { id: '23', lastNodeExecuted: 'Call token=abc123secret', error: { message: 'oops' } },
      workflow: { id: WF_GROOMING, name: 'Ticket Grooming' },
    },
    lookup: {},
  });
  assert.equal(keyed.data.stage, 'Call token=[redacted]');
  // Free text keeps the backstop.
  assert.equal(redactText('x a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8 y'), 'x [redacted] y');
});

test('redaction covers *_token keys, x-api-key and password values, URL credentials, session ids and the whole Authorization credential', () => {
  const cases = [
    ['refresh_token=rt-abc123', 'rt-abc123'],
    ['"access_token": "at.abc.123"', 'at.abc.123'],
    ['id_token: idt0kenvalue', 'idt0kenvalue'],
    ['client_token=ct-998877', 'ct-998877'],
    ['x-api-key plane_api_0123', 'plane_api_0123'],
    ['X-Api-Key: plane_api_4567', 'plane_api_4567'],
    ['password hunter2', 'hunter2'],
    ['passwd s3cretpw', 's3cretpw'],
    ['postgres://wax:pgpass99@db.delo.sh:5432/tx', 'pgpass99'],
    ['nats://bb:natspw77@nats:4222', 'natspw77'],
    ['amqp://user:p@ssw0rd@rabbit/vhost', 'ssw0rd'],
    ['redis://:redispw55@cache:6379', 'redispw55'],
    ['Cookie: session=sess-42abc; theme=dark', 'sess-42abc'],
    ['Authorization: Bearer xyz12', 'xyz12'],
    ['authorization: bearer tok:with:colons', 'with:colons'],
    ['Authorization: Basic dXNlcjpwYXNzd29yZA==', 'dXNlcjpwYXNzd29yZA'],
  ];
  for (const [text, secret] of cases) {
    const out = redactText(`before ${text} after`);
    assert.ok(!out.includes(secret), `${text} -> ${out}`);
    assert.match(out, /^before /, out);
    assert.match(out, / after$/, out);
    assert.equal(redactText(out), out, `idempotent: ${out}`);
  }
  assert.equal(redactText('Authorization: Bearer xyz12'), 'Authorization: Bearer [redacted]');
  assert.equal(redactText('postgres://wax:pgpass99@db.delo.sh/tx'), 'postgres://[redacted]@db.delo.sh/tx');
  // Prose that merely mentions a word is left alone where it has no value shape.
  assert.equal(redactText('Authorization failed - please check your credentials'), 'Authorization failed - please check your credentials');
  assert.equal(redactText('skills-sync-maintenance-2026'), 'skills-sync-maintenance-2026');
});

test('classification without an httpCode: 502 and 504 by name, refused connections, no property-access statuses', () => {
  const cases = [
    [{ message: 'Bad gateway' }, 'upstream_unavailable', 502],
    [{ message: '502 Bad Gateway' }, 'upstream_unavailable', 502],
    [{ message: 'Gateway timeout' }, 'upstream_unavailable', 504],
    [{ message: 'Gateway Time-out' }, 'upstream_unavailable', 504],
    [{ message: 'Service unavailable' }, 'upstream_unavailable', 503],
    [{ message: 'The service refused the connection - perhaps it is offline' }, 'upstream_unavailable', null],
    [{ message: "Cannot read properties of undefined (reading 'status 500')" }, 'node_error', null],
    [{ message: "Cannot read properties of undefined (reading 'timeout')" }, 'node_error', null],
  ];
  for (const [error, klass, status] of cases) {
    const got = classOf(error);
    assert.equal(got.class, klass, JSON.stringify(error));
    assert.equal(got.http_status, status, JSON.stringify(error));
  }
});

test('Retry-After is delta-seconds or an HTTP-date against now, never a date read as seconds', () => {
  const after = (error) => failing(error).data.error.retry_after_seconds;
  assert.equal(after({ message: 'HTTP 429 Retry-After: Thu, 05 Nov 2026 20:01:30 GMT' }), 90);
  assert.equal(
    after({ message: 'HTTP 429', cause: { response: { headers: { 'retry-after': 'Thu, 05 Nov 2026 20:00:10 GMT' } } } }),
    10,
  );
  assert.equal(after({ message: 'HTTP 429 Retry-After: Wed, 04 Nov 2026 07:28:00 GMT' }), 0, 'a past date is now');
  assert.equal(after({ message: 'HTTP 429 Retry-After: 21 Oct 2026' }), null, 'neither form');
  assert.equal(after({ message: 'HTTP 429', cause: { headers: { 'Retry-After': 'soon' } } }), null);
  assert.equal(after({ message: 'HTTP 429 Retry-After: 30' }), 30);
  assert.equal(after({ message: 'HTTP 429 retry after 45 seconds' }), 45);
  assert.equal(after({ message: 'HTTP 429 {"retry_after": 12}' }), 12);
  assert.match(failing({ message: 'HTTP 429 Retry-After: Thu, 05 Nov 2026 20:01:30 GMT' }).data.recovery.hint, /Wait 90s first/);
});

test('a Plane 403 that names membership, permission or workspace is fix_route and names both possibilities', () => {
  for (const body of [
    '{"detail":"You do not have permission to perform this action."}',
    '{"error":"You are not a member of this workspace"}',
    '{"error":"Only project members can view this issue"}',
  ]) {
    const error = structuredClone(GROOMED_ERROR);
    error.execution.error.message = 'HTTP 403 Forbidden';
    error.execution.error.description = body;
    error.execution.error.stack = error.execution.error.stack.replace('HTTP 429', 'HTTP 403');
    const { data } = reportValid({ errorPayload: error, lookup: GROOMED_LOOKUP });
    assert.equal(data.error.class, 'auth_failed', body);
    assert.equal(data.error.http_status, 403, body);
    assert.equal(data.error.credential_scope, 'fleet_shared', body);
    assert.equal(data.recovery.action, 'fix_route', body);
    assert.match(data.recovery.hint, /member/i, body);
    assert.match(data.recovery.hint, /permission/i, body);
    assert.match(data.recovery.hint, /rotate/i, body);
    assert.match(data.recovery.hint, /TpxVVgOnmjHaE6h7/, body);
    assert.match(data.recovery.hint, new RegExp(HEYMA_BOARD), body);
    assert.ok(data.recovery.hint.length <= 400);
  }
});

test('chip: the owner row fills the repo, so the ordering key is task:<repo>:<ticket_id>', () => {
  const error = structuredClone(CHIP_ERROR);
  error.execution.error.stack = `Error: Chip — List Labels failed for HEYMA-1 (board ${HEYMA_BOARD}): HTTP 500\n    at x`;
  const report = reportValid({ errorPayload: error, lookup: CHIP_LOOKUP });
  assert.equal(report.data.target_agent_id, 'heyma-pm');
  assert.equal(report.data.context.repo, 'transcription-queue');
  assert.equal(report.data.context.ticket_id, HEYMA_TICKET);
  assert.equal(report.orderingKey, `task:transcription-queue:${HEYMA_TICKET}`);
});

test('chip: a stack that names the ticket by UUID matches the sweep item by ticketId', () => {
  const uuid = 'c0ffee00-0000-4000-8000-000000000147';
  const error = structuredClone(CHIP_ERROR);
  error.execution.error.stack = `Error: Chip — List Labels failed for ${uuid} (board ${GRUV_BOARD}): HTTP 500\n    at x`;
  const { data } = reportValid({ errorPayload: error, lookup: CHIP_LOOKUP });
  assert.equal(data.context.ticket_key, 'GRUV-147');
  assert.equal(data.context.ticket_id, uuid);
  assert.equal(data.target_agent_id, 'gruv-pm');
  assert.equal(data.links[0].url, 'https://plane.delo.sh/automaticai/browse/GRUV-147/');
});

test('chip: a UUID nobody can name is ticket_id, never ticket_key, with no broken browse link', () => {
  const uuid = 'deadbeef-0000-4000-8000-000000000001';
  const error = structuredClone(CHIP_ERROR);
  error.execution.error.stack = `Error: Chip — List Labels failed for ${uuid} (board ${GRUV_BOARD}): HTTP 500\n    at x`;
  const report = reportValid({ errorPayload: error, lookup: CHIP_LOOKUP });
  assert.equal(report.data.context.ticket_key, null);
  assert.equal(report.data.context.ticket_id, uuid);
  assert.deepEqual(report.data.links.map((link) => link.rel), ['run']);
  assert.ok(!JSON.stringify(report).includes(`/browse/${uuid}`));
  assert.equal(report.page.click, CHIP_ERROR.execution.url);
  assert.equal(report.page.title, `Lane failed: ${uuid}`);
  // Same rule off the chip: a stack that names a UUID is the ticket id.
  const groomed = structuredClone(GROOMED_ERROR);
  groomed.execution.error.stack = `Error: Read Ticket failed for ${uuid} (board ${HEYMA_BOARD}): HTTP 429\n    at x`;
  const off = reportValid({ errorPayload: groomed, lookup: { error: { message: 'x' } } }).data;
  assert.equal(off.context.ticket_key, null);
  assert.equal(off.context.ticket_id, uuid);
  assert.ok(!(off.links || []).some((link) => link.rel === 'page'));
});

test('the lane branch never throws when the registryFile or connection parameter fails to resolve', async (t) => {
  const fallback = await tempRegistry(t, REGISTRY);
  const saved = process.env.HERMES_AGENTS_REGISTRY;
  process.env.HERMES_AGENTS_REGISTRY = fallback;
  t.after(() => {
    if (saved === undefined) delete process.env.HERMES_AGENTS_REGISTRY;
    else process.env.HERMES_AGENTS_REGISTRY = saved;
  });
  const rec = recorder();
  const broken = new Error('ExpressionError: invalid syntax');
  const [published, notPublished] = await Fleet.prototype.execute.call(
    context(
      { operation: 'reportLaneFailure', errorPayload: GROOMED_ERROR, registryFile: broken, connection: broken },
      [{ json: GROOMED_LOOKUP }],
    ),
    rec.send,
  );
  assert.equal(published.length + notPublished.length, 1);
  assert.equal(published[0].json.event.data.context.ticket_key, 'HEYMA-12');
  assert.equal(published[0].json.event.data.target_agent_id, 'heyma-pm');
});

test('an empty Execution Lookup input still yields one report, built from the error payload alone', async (t) => {
  const rec = recorder();
  const [published, notPublished] = await runNode(t, { errorPayload: GROOMED_ERROR }, [], rec.send);
  assert.equal(published.length, 1);
  assert.equal(notPublished.length, 0);
  const { data } = published[0].json.event;
  assert.equal(data.run_id, '632616');
  assert.equal(data.workflow, 'ticket-delegation-groomed');
  assert.equal(data.context.ticket_key, 'HEYMA-12');
  assert.equal(data.target_agent_id, 'heyma-pm');
  assert.equal(published[0].pairedItem, undefined, 'there is no input item to pair with');
  validateEnvelope(TYPE, published[0].json.event);
});
