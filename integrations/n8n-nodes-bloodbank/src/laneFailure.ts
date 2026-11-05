/** A failed ticket-lifecycle lane run, as one validated Bloodbank fact.
 *
 * `reportLaneFailure` turns what n8n's Error Trigger hands the error workflow
 * (plus the failed execution's own run data, when the lookup worked) into the
 * data of `bloodbank.workflow.task.failed`: which lane died where, on which
 * ticket, who owns it, how the failure classifies, what re-drives it.
 *
 * This is the error path, so it never throws. Every section degrades to
 * "unknown" on garbage input and the report is still built: a lane that failed
 * for a malformed reason must not fail again in the thing that reports it.
 *
 * Nothing secret leaves here. Strings are redacted, the stack is read for
 * classification and never copied, and the registry is projected down to the
 * company identity and gateway consumer only (never a credential reference).
 */
import { createHash } from 'node:crypto';

import { resolveFleetAgentForBoard, ticketCorrelationId, ticketFactsFromEnvelope } from './fleet';
import type { FleetRoute, TicketFacts } from './fleet';
import { deterministicUuid } from './nats';
import { labelNames } from './scope';

export const TASK_FAILED_TYPE = 'bloodbank.workflow.task.failed';

/** n8n workflow id -> lane slug. The slug is `data.workflow`. */
export const DEFAULT_LANE_MAP: Readonly<Record<string, string>> = Object.freeze({
  '6wAGA5pdrmHLyhs2': 'ticket-grooming',
  '8mmqdMwQYA28ZwUj': 'ticket-delegation',
  l0J53w0TsS5PwETk: 'ticket-delegation-groomed',
  wWXgCZiiIBWaRRzE: 'ticket-pickup-chip',
});

export const DEFAULT_N8N_BASE = 'https://n8n.delo.sh';
export const PLANE_BASE = 'https://plane.delo.sh';
/** Id of the one n8n credential every lane Plane read uses. An id, not a secret. */
export const SHARED_PLANE_N8N_ID = 'TpxVVgOnmjHaE6h7';

export type ErrorClass =
  | 'rate_limited'
  | 'auth_failed'
  | 'not_found'
  | 'no_route'
  | 'ineligible'
  | 'invalid_payload'
  | 'upstream_unavailable'
  | 'timeout'
  | 'bus_unavailable'
  | 'node_error'
  | 'unknown';

export type CredentialScope = 'fleet_shared' | 'employee' | 'service' | 'none' | 'unknown';
export type RecoveryAction =
  | 'retry_execution'
  | 'requeue_ticket'
  | 'retry_or_requeue'
  | 'fix_route'
  | 'rotate_credential'
  | 'enroll_identity'
  | 'none';

export interface CompanyIdentity {
  subject: string;
  handle: string;
  email: string;
  status: 'active' | 'suspended' | 'retired';
  plane_attribution: 'native' | 'unbound';
}

export interface LanePage {
  title: string;
  message: string;
  click: string;
  priority: string;
  tags: string;
}

export interface LaneFailureReport {
  data: Record<string, unknown>;
  eventId: string;
  msgId: string;
  correlationId?: string;
  causationId?: string;
  observedAt?: string;
  orderingKey: string;
  page: LanePage;
}

export interface LaneFailureInput {
  /** The Error Trigger item (`{execution, workflow}` or `{trigger, workflow}`), or JSON text of it. */
  errorPayload?: unknown;
  /** The Execution Lookup item: the n8n API execution with data, or `{error}`. */
  lookup?: unknown;
  /** Workflow id -> lane slug, as an object or JSON text. */
  laneMap?: unknown;
  /** The parsed Hermes agents registry, when it could be read. */
  registry?: unknown;
  now?: Date;
}

type Obj = Record<string, unknown>;

function obj(value: unknown): Obj | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Obj)
    : undefined;
}

function str(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

function orNull(value: string): string | null {
  return value || null;
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

/** JSON text or an object, as an object; anything else is empty. */
function asObject(value: unknown): Obj {
  if (typeof value === 'string') {
    try {
      return obj(JSON.parse(value)) || {};
    } catch {
      return {};
    }
  }
  return obj(value) || {};
}

export function slugify(name: string): string {
  const slug = name
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
    .replace(/-+$/g, '');
  return slug || 'unknown-workflow';
}

export function parseLaneMap(value: unknown): Record<string, string> {
  const given = asObject(value);
  const map: Record<string, string> = {};
  for (const [id, slug] of Object.entries(given)) {
    const text = str(slug);
    if (str(id) && text) map[id.trim()] = text;
  }
  return Object.keys(map).length ? map : { ...DEFAULT_LANE_MAP };
}

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Strip anything secret-shaped from one string. UUIDs are ids, not secrets. */
export function redactText(value: string): string {
  return value
    .replace(/\bop:\/\/[^\s"'`)\]>,]+/gi, 'op://[redacted]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]{6,}/gi, 'Bearer [redacted]')
    .replace(/\bBasic\s+[A-Za-z0-9+/=]{8,}/g, 'Basic [redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/g, '[redacted]')
    .replace(
      /\b(x-api-key|api[_-]?key|authorization|access[_-]?token|auth[_-]?token|token|secret|password|passwd)(["']?\s*[:=]\s*["']?)[^\s"'&,;}]+/gi,
      '$1$2[redacted]',
    )
    .replace(/([?&](?:api[_-]?key|key|token|access_token|secret|sig|signature)=)[^&\s"']+/gi, '$1[redacted]')
    .replace(/\b(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[redacted]@')
    .replace(/\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|glpat|AKIA)[-_A-Za-z0-9]{12,}/g, '[redacted]')
    .replace(/[A-Za-z0-9_+=-]{32,}/g, (token) =>
      UUID.test(token) || !/\d/.test(token) || !/[A-Za-z]/.test(token) ? token : '[redacted]',
    );
}

function cap(value: string, max: number): string {
  const flat = value.replace(/\s+/g, ' ').trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1).trimEnd()}…`;
}

function redactDeep(value: unknown): unknown {
  if (typeof value === 'string') return redactText(value);
  if (Array.isArray(value)) return value.map(redactDeep);
  const o = obj(value);
  if (!o) return value;
  return Object.fromEntries(Object.entries(o).map(([k, v]) => [k, redactDeep(v)]));
}

// ---------------------------------------------------------------------------
// Reading the failed execution
// ---------------------------------------------------------------------------

type Lane = 'grooming' | 'delegation' | 'groomed' | 'chip' | 'unknown';

const LANE_BY_SLUG: Record<string, Lane> = {
  'ticket-grooming': 'grooming',
  'ticket-delegation': 'delegation',
  'ticket-delegation-groomed': 'groomed',
  'ticket-pickup-chip': 'chip',
};

const LANE_TRIGGER_NODE: Array<[string, Lane]> = [
  ['onTicketCreated', 'grooming'],
  ['onTicketTransitioned', 'delegation'],
  ['onGroomingCompleted', 'groomed'],
  ['Chip — Target', 'chip'],
];

/** The invocation purpose a lane's commands carry: `context.reason`. */
const LANE_PURPOSE: Record<Lane, string | undefined> = {
  grooming: 'ticket-grooming',
  delegation: 'ticket-delegation',
  groomed: 'ticket-delegation',
  chip: 'ticket-pickup-chip',
  unknown: undefined,
};

function runDataOf(lookup: Obj): Obj {
  const data = obj(lookup.data);
  const result = data && obj(data.resultData);
  return (result && obj(result.runData)) || {};
}

/** The json of every item a node emitted on one output (first run only). */
function nodeItems(runData: Obj, node: string, output = 0): Obj[] {
  const runs = runData[node];
  const first = Array.isArray(runs) ? obj(runs[0]) : undefined;
  const main = first && obj(first.data)?.main;
  const branch = Array.isArray(main) ? main[output] : undefined;
  if (!Array.isArray(branch)) return [];
  return branch.map((item) => obj(obj(item)?.json)).filter((json): json is Obj => Boolean(json));
}

function detectLane(slug: string, runData: Obj): Lane {
  const bySlug = LANE_BY_SLUG[slug];
  if (bySlug) return bySlug;
  for (const [node, lane] of LANE_TRIGGER_NODE) if (runData[node] !== undefined) return lane;
  return 'unknown';
}

interface Extracted {
  facts: TicketFacts;
  trigger?: Obj;
  targetAgentId: string;
}

const EMPTY_FACTS: TicketFacts = Object.freeze({
  repo: '',
  boardId: '',
  ticketKey: '',
  ticketId: '',
  title: '',
  workspace: '',
  phase: '',
  previousPhase: '',
  providerEventType: '',
  url: '',
  labels: [],
}) as TicketFacts;

/** The failing ticket named on line 1 of the stack: `... failed for KEY (board <uuid>)`.
 *
 * n8n splits a Code node's message at the last ": ", so the message is not
 * reliable; the first stack line is. A sweep carries ~98 tickets, so this is
 * the only trustworthy way to say WHICH one failed.
 */
const FAILED_FOR = /failed for (\S+) \(board ([0-9a-f-]+)\)/i;

function failedTicketFromStack(stack: string): { key: string; board: string } | undefined {
  const line = stack.split('\n', 1)[0] || '';
  const match = FAILED_FOR.exec(line);
  return match ? { key: match[1], board: match[2] } : undefined;
}

function extractFacts(lane: Lane, runData: Obj, stack: string): Extracted {
  let facts: TicketFacts = { ...EMPTY_FACTS, labels: [] };
  let trigger: Obj | undefined;
  let targetAgentId = '';

  if (lane === 'grooming' || lane === 'delegation') {
    trigger = nodeItems(runData, lane === 'grooming' ? 'onTicketCreated' : 'onTicketTransitioned')[0];
    if (trigger) facts = ticketFactsFromEnvelope(trigger);
  } else if (lane === 'groomed') {
    trigger = nodeItems(runData, 'onGroomingCompleted')[0];
    const data = obj(trigger?.data);
    if (data) {
      facts = ticketFactsFromEnvelope(obj(data.context) || {});
      targetAgentId = str(data.target_agent_id);
    }
    // The live labels (what the owner is picked by on a shared board) come
    // from the ticket the lane just re-read, when it got that far.
    const live = nodeItems(runData, 'Read Ticket')[0];
    const liveLabels = labelNames(live?.labels);
    if (liveLabels.length) facts = { ...facts, labels: liveLabels };
  } else if (lane === 'chip') {
    const failing = failedTicketFromStack(stack);
    if (failing) {
      const item = nodeItems(runData, 'Chip — Target').find(
        (candidate) => str(candidate.ticketKey) === failing.key,
      );
      facts = {
        ...facts,
        ticketKey: failing.key,
        boardId: failing.board,
        ticketId: str(item?.ticketId),
        workspace: str(item?.ws),
      };
      if (item && str(item.board)) facts.boardId = str(item.board);
      targetAgentId = str(item?.agentId);
    }
    return { facts, trigger: undefined, targetAgentId };
  } else {
    // An unknown workflow: take the first item that names a ticket.
    for (const node of Object.keys(runData)) {
      for (const json of nodeItems(runData, node)) {
        const found = ticketFactsFromEnvelope({
          ...json,
          ticket_key: str(json.ticketKey) || json.ticket_key,
          workspace: str(json.ws) || json.workspace,
        });
        if (found.ticketKey) {
          facts = found;
          break;
        }
      }
      if (facts.ticketKey) break;
    }
  }

  if (!facts.ticketKey) {
    const failing = failedTicketFromStack(stack);
    if (failing) {
      facts = { ...facts, ticketKey: failing.key, boardId: facts.boardId || failing.board };
    }
  }
  return { facts, trigger, targetAgentId };
}

// ---------------------------------------------------------------------------
// Classifying the error
// ---------------------------------------------------------------------------

const SCOPES = new Set<CredentialScope>(['fleet_shared', 'employee', 'service', 'none', 'unknown']);

interface ErrorFacts {
  message: string;
  description: string;
  stackLine: string;
  httpCode: number | null;
  text: string;
}

function readError(errorPayload: Obj, lookup: Obj): ErrorFacts {
  const exe = obj(errorPayload.execution);
  const trigger = obj(errorPayload.trigger);
  const looked = obj(obj(obj(lookup.data)?.resultData)?.error);
  const err = obj(exe?.error) || obj(trigger?.error) || looked || {};
  const message = str(err.message);
  const description = str(err.description);
  const stackLine = (str(err.stack).split('\n', 1)[0] || '').trim();
  const rawCode = err.httpCode ?? err.statusCode ?? err.code;
  const code = Number(typeof rawCode === 'string' ? rawCode.trim() : rawCode);
  const httpCode = Number.isInteger(code) && code >= 100 && code <= 599 ? code : null;
  return {
    message,
    description,
    stackLine,
    httpCode,
    text: [message, description, stackLine].filter(Boolean).join(' | '),
  };
}

function httpStatusFrom(error: ErrorFacts): number | null {
  if (error.httpCode) return error.httpCode;
  const patterns = [
    /\bHTTP\s*(?:status\s*)?(?:code\s*)?(\d{3})\b/i,
    /\bstatus(?:\s*code)?\s*[:=]?\s*(\d{3})\b/i,
    /(?:^|\|\s*)(\d{3})\s*[-:]\s/,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(error.text);
    const status = match ? Number(match[1]) : NaN;
    if (status >= 100 && status <= 599) return status;
  }
  const t = error.text;
  if (/too many requests|try spacing your requests/i.test(t)) return 429;
  if (/authorization failed|unauthorized/i.test(t)) return 401;
  if (/forbidden/i.test(t)) return 403;
  if (/could not be found|not found/i.test(t) && /resource|requesting/i.test(t)) return 404;
  if (/service unavailable|bad gateway|gateway timeout/i.test(t)) return 503;
  return null;
}

function retryAfterSeconds(error: ErrorFacts, errorObject: Obj): number | null {
  const fromText = /retry[-_ ]?after[^0-9]{0,15}(\d{1,6})\b/i.exec(error.text);
  if (fromText) return Number(fromText[1]);
  // n8n keeps the response headers on `cause`/`context` for an HTTP failure.
  const seen = new Set<unknown>();
  const find = (value: unknown, depth: number): number | null => {
    const o = obj(value);
    if (!o || depth > 4 || seen.has(o)) return null;
    seen.add(o);
    for (const [key, inner] of Object.entries(o)) {
      if (key === 'stack') continue;
      if (key.toLowerCase() === 'retry-after') {
        const n = Number(str(inner));
        if (Number.isInteger(n) && n >= 0) return n;
      }
      const deeper = find(inner, depth + 1);
      if (deeper !== null) return deeper;
    }
    return null;
  };
  return find(errorObject, 0);
}

interface Classified {
  class: ErrorClass;
  http_status: number | null;
  upstream: string | null;
  credential_scope: CredentialScope;
  retry_after_seconds: number | null;
}

function classify(
  error: ErrorFacts,
  errorObject: Obj,
  failingNodeType: string,
  planeNode: boolean,
  scopeHint: CredentialScope | undefined,
): Classified {
  const t = error.text;
  const status = httpStatusFrom(error);
  const bloodbankNode = failingNodeType.startsWith('n8n-nodes-bloodbank.');
  const nats =
    /\bnats|jetstream|no responders/i.test(t) ||
    (bloodbankNode && /ECONNREFUSED|ETIMEDOUT|timed? ?out|timeout|connect|CONNECTION/i.test(t));
  const planeText = /\bplane\b|plane\.delo\.sh/i.test(t);

  let klass: ErrorClass;
  if (nats) klass = 'bus_unavailable';
  else if (status !== null && status >= 400) {
    if (status === 429) klass = 'rate_limited';
    else if (status === 401 || status === 403) klass = 'auth_failed';
    else if (status === 404 || status === 410) klass = 'not_found';
    else if (status === 408) klass = 'timeout';
    else if (status === 400 || status === 422) klass = 'invalid_payload';
    else if (status >= 500) klass = 'upstream_unavailable';
    else klass = 'node_error';
  } else if (/ETIMEDOUT|ESOCKETTIMEDOUT|timed? ?out|timeout/i.test(t)) klass = 'timeout';
  else if (/ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|socket hang up|fetch failed|getaddrinfo/i.test(t)) {
    klass = 'upstream_unavailable';
  } else if (/no registry entry matches board|no fallback agent|no agent owns|has no agents mapping/i.test(t)) {
    klass = 'no_route';
  } else if (
    /bloodbank\.enabled|has no (?:profile_name|bloodbank block)|gateway_scope|target_agent_id mismatch/i.test(t)
  ) {
    klass = 'ineligible';
  } else if (
    /carries neither data\.repo|must be a non-empty string|must be valid JSON|must be a JSON object|schema validation failed/i.test(t)
  ) {
    klass = 'invalid_payload';
  } else klass = t ? 'node_error' : 'unknown';

  const upstream = klass === 'bus_unavailable' ? 'nats' : planeNode || planeText ? 'plane' : null;
  let scope: CredentialScope;
  if (scopeHint) scope = scopeHint;
  else if (upstream === 'plane') scope = 'fleet_shared';
  else if (upstream === 'nats' || klass === 'no_route' || klass === 'ineligible' || klass === 'invalid_payload') {
    scope = 'none';
  } else scope = 'unknown';

  return {
    class: klass,
    http_status: status !== null && status >= 400 ? status : null,
    upstream,
    credential_scope: scope,
    retry_after_seconds: retryAfterSeconds(error, errorObject),
  };
}

// ---------------------------------------------------------------------------
// The owner, from the registry
// ---------------------------------------------------------------------------

const IDENTITY_STATUS = new Set(['active', 'suspended', 'retired']);

function registryRow(registry: unknown, agentId: string): Obj | undefined {
  if (!agentId) return undefined;
  const agents = obj(obj(registry)?.agents);
  return agents ? obj(agents[agentId]) : undefined;
}

/** The registry row's `company_identity`, projected; null when absent or malformed.
 *
 * Only enrolled employees carry one. A block that does not satisfy the
 * event's own schema is reported as not enrolled rather than half-copied, so
 * a bad row can never make the failure report itself fail validation.
 */
export function companyIdentityOf(entry: unknown): CompanyIdentity | null {
  const identity = obj(obj(entry)?.company_identity);
  if (!identity) return null;
  const subject = str(identity.subject);
  const handle = str(identity.handle);
  const email = str(identity.email);
  const status = str(identity.status);
  if (!UUID.test(subject) || !handle || !/^[^@\s]+@agents\.delo\.sh$/.test(email) || !IDENTITY_STATUS.has(status)) {
    return null;
  }
  const plane = obj(identity.plane);
  return {
    subject,
    handle,
    email,
    status: status as CompanyIdentity['status'],
    plane_attribution: str(plane?.attribution_mode) === 'native' ? 'native' : 'unbound',
  };
}

/** The AutomaticAI gateway consumer a row's desk uses, or null when unknown. */
export function gatewayConsumerOf(entry: unknown): string | null {
  return orNull(str(obj(entry)?.gateway_consumer));
}

/** An agent's company identity straight from a loaded registry (null if none). */
export function ownerCompanyIdentity(registry: unknown, agentId: string): CompanyIdentity | null {
  return companyIdentityOf(registryRow(registry, agentId));
}

// ---------------------------------------------------------------------------
// Recovery
// ---------------------------------------------------------------------------

function originOf(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.origin : undefined;
  } catch {
    return undefined;
  }
}

interface RecoveryInput {
  lane: Lane;
  executionId: string;
  executionReal: boolean;
  executionUrl: string;
  ticketKey: string;
  phase: string;
  klass: Classified;
  identity: CompanyIdentity | null;
  ownerId: string;
  boardId: string;
}

function recovery(input: RecoveryInput): { action: RecoveryAction; hint: string } {
  const { lane, klass } = input;
  const retryUrl = `${originOf(input.executionUrl) || DEFAULT_N8N_BASE}/api/v1/executions/${input.executionId}/retry`;
  const retry = `Retry: POST ${retryUrl} (works only while the run data exists, ~7h).`;
  const bounce = input.ticketKey
    ? `Or requeue: px move ${input.ticketKey} Backlog -m "lane failed (execution ${input.executionId})" && px move ${input.ticketKey} Todo (one ticket at a time on WIP=1 boards).`
    : '';
  const wait = klass.retry_after_seconds ? ` Wait ${klass.retry_after_seconds}s first (Retry-After).` : '';
  const done = (action: RecoveryAction, ...parts: string[]) => ({
    action,
    hint: cap(parts.filter(Boolean).join(' '), 400),
  });

  if (klass.class === 'auth_failed' && klass.credential_scope !== 'unknown' && klass.credential_scope !== 'none') {
    if (klass.credential_scope === 'employee') {
      if (!input.identity) {
        return done(
          'enroll_identity',
          `${input.ownerId || 'The owner'} has no company identity, so its own credential cannot be traced: enroll it (role.yaml), then re-drive.`,
          input.executionReal ? retry : '',
        );
      }
      return done(
        'rotate_credential',
        `Rotate ${input.identity.handle}'s credential (employee subject ${input.identity.subject}), then re-drive.`,
        input.executionReal ? retry : '',
      );
    }
    return done(
      'rotate_credential',
      `Rotate the shared ${klass.upstream || 'upstream'} credential (n8n ${SHARED_PLANE_N8N_ID}), then re-drive.`,
      input.executionReal ? retry : '',
    );
  }
  if (klass.class === 'no_route' || klass.class === 'ineligible') {
    return done(
      'fix_route',
      `Fix the fleet registry route for ${input.boardId ? `board ${input.boardId}` : 'this ticket'}${
        input.ownerId ? ` (agent ${input.ownerId})` : ''
      } in ~/.hermes/agents-registry.yaml, then re-drive.`,
      input.executionReal ? retry : '',
      bounce,
    );
  }

  if (lane === 'chip') {
    return { action: 'none', hint: 'None: the hourly sweep heals it.' };
  }
  if (!input.executionReal) {
    return done(
      'none',
      'The trigger failed before an execution existed, so there is nothing to retry: check the workflow in n8n.',
    );
  }
  if (lane === 'grooming') {
    const phase = input.phase.toLowerCase();
    if (phase === 'todo') return done('retry_or_requeue', wait, retry, bounce);
    return done(
      'retry_execution',
      wait,
      retry,
      phase === 'backlog'
        ? 'Backlog-born ticket: retry the execution only, do NOT bounce Backlog->Todo (that promotes it).'
        : 'Do not bounce Backlog->Todo unless the ticket was created in Todo (that promotes it).',
    );
  }
  if (lane === 'delegation' || lane === 'groomed') {
    return input.ticketKey
      ? done('retry_or_requeue', wait, retry, bounce)
      : done('retry_execution', wait, retry);
  }
  return done('retry_execution', wait, retry);
}

// ---------------------------------------------------------------------------
// The report
// ---------------------------------------------------------------------------

function ticketPage(workspace: string, key: string): string | undefined {
  return workspace && key && /^[A-Za-z0-9_.-]+$/.test(workspace) && /^[A-Za-z0-9_.-]+$/.test(key)
    ? `${PLANE_BASE}/${workspace}/browse/${key}/`
    : undefined;
}

function httpsUrl(value: string): string | undefined {
  return /^https:\/\/[^\s"<>]+$/.test(value) && value.length <= 600 ? value : undefined;
}

function nodeTypeOf(lookup: Obj, name: string): { type: string; url: string } {
  const workflow = obj(lookup.workflowData);
  const nodes = Array.isArray(workflow?.nodes) ? (workflow!.nodes as unknown[]) : [];
  const found = nodes.map(obj).find((node) => node && str(node.name) === name);
  return {
    type: str(found?.type),
    url: str(obj(found?.parameters)?.url),
  };
}

/** Build the failure report. Never throws. */
export function reportLaneFailure(input: LaneFailureInput): LaneFailureReport {
  const now = input.now || new Date();
  const errorPayload = safe(() => asObject(input.errorPayload), {});
  const lookup = safe(() => asObject(input.lookup), {});

  const exe = obj(errorPayload.execution) || {};
  const triggerFailure = obj(errorPayload.trigger) || {};
  const wf = obj(errorPayload.workflow) || {};
  const workflowData = obj(lookup.workflowData) || {};

  const workflowId = str(wf.id) || str(lookup.workflowId) || str(workflowData.id);
  const workflowName = str(wf.name) || str(workflowData.name);
  const laneMap = safe(() => parseLaneMap(input.laneMap), { ...DEFAULT_LANE_MAP });
  const slug = laneMap[workflowId] || (workflowName ? slugify(workflowName) : 'unknown-workflow');

  const realExecutionId = str(exe.id) || str(lookup.id);
  const runData = safe(() => runDataOf(lookup), {});
  const error = safe(() => readError(errorPayload, lookup), {
    message: '',
    description: '',
    stackLine: '',
    httpCode: null,
    text: '',
  });
  const errorObject = safe(
    () => obj(exe.error) || obj(triggerFailure.error) || obj(obj(obj(lookup.data)?.resultData)?.error) || {},
    {},
  );
  const stack = safe(() => str(errorObject.stack), '');

  // A failure inside the trigger itself has no execution. The id is then a
  // stable function of the workflow and the failure (per hour), so a trigger
  // failing every minute is one fact an hour on the bus, not sixty.
  const executionId =
    realExecutionId ||
    `trigger-${createHash('sha1')
      .update(`${workflowId}\n${error.message}\n${now.toISOString().slice(0, 13)}`)
      .digest('hex')
      .slice(0, 12)}`;

  const lane = safe(() => detectLane(slug, runData), 'unknown' as Lane);
  const extracted = safe(() => extractFacts(lane, runData, stack), {
    facts: { ...EMPTY_FACTS, labels: [] as string[] },
    trigger: undefined,
    targetAgentId: '',
  } as Extracted);
  let facts = extracted.facts;

  // The failing node, and whether it talks to Plane.
  const stage =
    str(exe.lastNodeExecuted) ||
    str(obj(obj(lookup.data)?.resultData)?.lastNodeExecuted) ||
    (realExecutionId ? 'unknown' : 'trigger');
  const namedInMessage = /^(.*?) failed for /.exec(error.stackLine.replace(/^\w*Error:\s*/, ''))?.[1]?.trim() || '';
  const probe = (name: string) => (name ? nodeTypeOf(lookup, name) : { type: '', url: '' });
  const stageNode = probe(stage);
  const namedNode = probe(namedInMessage);
  const planeNode = [stageNode, namedNode].some((node) => /plane/i.test(node.url));
  const failingType = namedNode.type || stageNode.type;

  const scopeHint = safe(() => {
    const hint = str(errorPayload.credential_scope) as CredentialScope;
    return SCOPES.has(hint) ? hint : undefined;
  }, undefined);
  const klass = safe(
    () => classify(error, errorObject, failingType, planeNode, scopeHint),
    {
      class: 'unknown',
      http_status: null,
      upstream: null,
      credential_scope: 'unknown',
      retry_after_seconds: null,
    } as Classified,
  );

  // The owner. The groomed lane's trigger names it; the others resolve by
  // board (then repo), with labels from the run data when known.
  const registry = input.registry;
  const owner: { route?: FleetRoute; id: string; matchedBy: string | null } = {
    id: '',
    matchedBy: null,
  };
  safe(() => {
    if (extracted.targetAgentId) {
      owner.id = extracted.targetAgentId;
      return;
    }
    if (registry && (facts.boardId || facts.repo)) {
      owner.route = resolveFleetAgentForBoard(registry, facts.boardId, facts.repo, facts.labels);
      owner.matchedBy = owner.route.matchedBy;
      owner.id = owner.route.code === 'no_route' ? '' : owner.route.agentId;
    }
  }, undefined);
  const ownerId = owner.id;
  const matchedBy = owner.matchedBy;
  const route = owner.route;
  const row = registryRow(registry, ownerId);
  const identity = safe(() => companyIdentityOf(row), null);
  const gatewayConsumer = safe(() => gatewayConsumerOf(row), null);
  if (!facts.workspace && route?.workspace) facts = { ...facts, workspace: route.workspace };
  if (!facts.workspace) {
    const planeWs = str(obj(row?.plane)?.workspace);
    if (planeWs) facts = { ...facts, workspace: planeWs };
  }

  const executionUrl = httpsUrl(str(exe.url)) || '';
  const rec = safe(
    () =>
      recovery({
        lane,
        executionId,
        executionReal: Boolean(realExecutionId),
        executionUrl,
        ticketKey: facts.ticketKey,
        phase: facts.phase,
        klass,
        identity,
        ownerId,
        boardId: facts.boardId,
      }),
    { action: 'none' as RecoveryAction, hint: 'No recovery could be derived; open the execution in n8n.' },
  );

  // Ids: a pure function of the failed run, so a redelivered error-workflow
  // run republishes the same message and JetStream drops it.
  const eventId = deterministicUuid(`workflow.task.failed:${workflowId || 'unknown'}:${executionId}`);
  const trigger = extracted.trigger || {};
  const triggerId = str(trigger.id);
  const correlationId =
    str(trigger.correlationid) ||
    safe(
      () =>
        facts.boardId && (facts.ticketId || facts.ticketKey)
          ? ticketCorrelationId(facts.boardId, facts.ticketId || facts.ticketKey)
          : '',
      '',
    );
  const orderingKey = facts.ticketId
    ? `task:${facts.repo || facts.boardId || 'unknown'}:${facts.ticketId}`
    : `workflow:${workflowId || 'unknown'}:${executionId}`;

  const message = error.message || str(error.description) || 'unknown error';
  const reason = cap(redactText(message), 500) || 'unknown error';
  const ticketRef = facts.ticketKey || workflowName || slug;
  const klassLabel = `${klass.class}${klass.http_status ? `, HTTP ${klass.http_status}` : ''}`;
  const summary = cap(
    redactText(`${ticketRef}: ${workflowName || slug} failed at ${stage} (${klassLabel})`),
    200,
  );

  const links: Array<{ rel: 'page' | 'run'; label: string; url: string }> = [];
  const page = ticketPage(facts.workspace, facts.ticketKey);
  if (page) links.push({ rel: 'page', label: 'Ticket', url: page });
  if (executionUrl) links.push({ rel: 'run', label: 'Execution', url: executionUrl });

  const data: Record<string, unknown> = {
    workflow: slug,
    run_id: executionId,
    stage,
    reason,
    summary,
    lane: {
      engine: 'n8n',
      workflow_id: workflowId || 'unknown',
      workflow_name: orNull(workflowName),
      execution_id: executionId,
      execution_url: orNull(executionUrl),
      mode: orNull(str(exe.mode) || str(triggerFailure.mode) || str(lookup.mode)),
      retry_of: orNull(str(exe.retryOf) || str(lookup.retryOf)),
      trigger_type: orNull(str(trigger.type)),
      trigger_event_id: orNull(triggerId),
    },
    target_agent_id: orNull(ownerId),
    matched_by: matchedBy,
    context: {
      reason: LANE_PURPOSE[lane] || slug,
      repo: orNull(facts.repo),
      ticket_key: orNull(facts.ticketKey),
      ticket_id: orNull(facts.ticketId),
      board_id: orNull(facts.boardId),
      workspace: orNull(facts.workspace),
      title: orNull(facts.title),
      phase: orNull(facts.phase),
      previous_phase: orNull(facts.previousPhase),
      provider_event_type: orNull(facts.providerEventType),
    },
    company_identity: identity,
    gateway_consumer: gatewayConsumer,
    error: klass,
    severity: lane === 'chip' ? 'low' : 'page',
    recovery: rec,
    redacted: true,
  };
  if (links.length) data.links = links;
  const clean = redactDeep(data) as Record<string, unknown>;

  const ownerLine = ownerId
    ? identity
      ? `Owner: ${ownerId} (${identity.handle}, ${identity.email}, ${identity.status})`
      : `Owner: ${ownerId} (no company identity)`
    : 'Owner: unknown';
  const lines = [
    `${workflowName || slug} failed at ${stage}: ${cap(redactText(message), 200)}`,
    facts.ticketKey
      ? `Ticket ${facts.ticketKey}${lane === 'chip' ? '' : ' was NOT handed on'}.`
      : 'Ticket unknown.',
    ownerLine,
    `Error: ${klassLabel}${klass.upstream ? ` via ${klass.upstream}` : ''}${
      klass.retry_after_seconds ? `, retry after ${klass.retry_after_seconds}s` : ''
    }`,
    `Recovery: ${rec.hint}`,
  ];
  const pageOut: LanePage = {
    title: cap(redactText(`Lane failed: ${ticketRef}`), 120),
    message: redactText(lines.join('\n')),
    click: page || executionUrl || `${DEFAULT_N8N_BASE}/`,
    priority: lane === 'chip' ? '2' : '4',
    tags: `${lane === 'chip' ? 'information_source' : 'warning'},lane-error,${slug}`,
  };

  return {
    data: clean,
    eventId,
    msgId: eventId,
    correlationId: correlationId || undefined,
    causationId: triggerId || undefined,
    observedAt: safe(() => {
      const stopped = str(lookup.stoppedAt);
      const parsed = stopped ? Date.parse(stopped) : NaN;
      return Number.isNaN(parsed) ? undefined : new Date(parsed).toISOString();
    }, undefined),
    orderingKey,
    page: pageOut,
  };
}
