import { createHmac, timingSafeEqual } from 'node:crypto';

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { deterministicUuid, publish } from '../../nats';
import {
  classifyPlaneWebhook,
  issueAsWebhookPayload,
  mergePlaneRoutes,
  planeRoutesFromRegistry,
  unboundRegistryProjectPaths,
} from '../../plane';
import type { NormalizedPlaneEvent, PlaneProjectRoute } from '../../plane';
import { boardFromManifest, loadProjectBoards } from '../../projects';
import {
  createdTicketIdsSince,
  planeReader as makePlaneReader,
  planReconcile,
  RECONCILE_DEFAULTS,
} from '../../reconcile';
import type { PlaneReader } from '../../reconcile';
import type { ProjectBoard, ProjectBoardsResult, RegistryFetch } from '../../projects';
import { hermesRegistryPath, loadHermesRegistry } from '../../registry';
import { cachedSecret, mayForceRefresh, opRead } from '../../secrets';
import type { SecretReader, SecretResult } from '../../secrets';
import { eventSchemas } from '../Bloodbank/eventSchemas';

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function parseWebhookSecretReferences(value: unknown): Record<string, string> {
  let parsed = value;
  if (typeof value === 'string') {
    try {
      parsed = value.trim() ? JSON.parse(value) : {};
    } catch {
      throw new Error('Webhook Secret References must be a JSON object');
    }
  }
  const references = record(parsed);
  if (!references) {
    throw new Error('Webhook Secret References must be a JSON object');
  }
  const normalized: Record<string, string> = {};
  for (const [webhookId, reference] of Object.entries(references)) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(webhookId)) {
      throw new Error('Webhook Secret References contains a malformed webhook id');
    }
    if (typeof reference !== 'string' || !/^(op|env):\/\//.test(reference.trim())) {
      throw new Error('Webhook Secret References values must use op:// or env:// references');
    }
    normalized[webhookId.toLowerCase()] = reference.trim();
  }
  return normalized;
}

export function secretReferenceForWebhook(
  payload: Record<string, unknown>,
  references: Record<string, string>,
  legacyReference = '',
): string {
  const configuredWebhookIds = Object.keys(references);
  if (!configuredWebhookIds.length) {
    if (!legacyReference.trim()) {
      throw new Error('No trusted Plane webhook secrets are configured');
    }
    return legacyReference.trim();
  }
  const webhookId = typeof payload.webhook_id === 'string' ? payload.webhook_id.toLowerCase() : '';
  const reference = references[webhookId];
  if (!reference) {
    throw new Error('Plane webhook id is not in the trusted secret allowlist');
  }
  return reference;
}

/** Resolve an op:// or env:// reference; op:// goes through the TTL cache. */
export async function resolveSecret(
  reference: string,
  read: SecretReader = opRead,
  force = false,
): Promise<SecretResult> {
  if (reference.startsWith('env://')) {
    const variable = reference.slice('env://'.length);
    if (!/^[A-Z][A-Z0-9_]*$/.test(variable)) {
      throw new Error('env:// secret references must name an uppercase environment variable');
    }
    const value = process.env[variable];
    if (!value) throw new Error(`secret environment variable ${variable} is not set`);
    return value.startsWith('op://') ? resolveSecret(value, read, force) : { value, source: 'fresh' };
  }
  if (reference.startsWith('op://')) {
    return cachedSecret(reference, read, { force });
  }
  throw new Error('Webhook Secret Reference must use op:// or env://; raw secrets are forbidden');
}

function headerValue(headers: Record<string, unknown>, name: string): string | undefined {
  const value = headers[name] ?? headers[name.toLowerCase()] ?? headers[name.toUpperCase()];
  if (Array.isArray(value)) return value.length ? String(value[0]) : undefined;
  return typeof value === 'string' && value ? value : undefined;
}

class SignatureMismatch extends Error {}

export function verifyHmac(rawBody: Buffer, headers: Record<string, unknown>, secret: string): void {
  const supplied = (
    headerValue(headers, 'x-plane-signature') ||
    headerValue(headers, 'x-hub-signature-256') ||
    ''
  ).replace(/^sha256=/i, '');
  if (!/^[0-9a-f]{64}$/i.test(supplied)) {
    throw new Error('missing or malformed Plane HMAC signature header');
  }
  const expected = createHmac('sha256', secret).update(rawBody).digest('hex');
  const left = Buffer.from(supplied.toLowerCase(), 'hex');
  const right = Buffer.from(expected, 'hex');
  if (left.length !== right.length || !timingSafeEqual(left, right)) {
    throw new SignatureMismatch('Plane HMAC signature mismatch');
  }
}

/** Verify with the cached secret; on a mismatch, re-read once and retry.
 *
 * A rotated secret looks exactly like a forged request against a cached value.
 * One forced re-read — rate-limited per reference — tells them apart without
 * turning a stream of bad requests back into a vault read per request.
 */
async function verifyWithSecret(
  reference: string,
  rawBody: Buffer,
  headers: Record<string, unknown>,
  read: SecretReader,
): Promise<SecretResult['source']> {
  const first = await resolveSecret(reference, read);
  try {
    verifyHmac(rawBody, headers, first.value);
    return first.source;
  } catch (error) {
    if (!(error instanceof SignatureMismatch) || first.source === 'fresh' || !mayForceRefresh(reference)) {
      throw error;
    }
  }
  const refreshed = await resolveSecret(reference, read, true);
  verifyHmac(rawBody, headers, refreshed.value);
  return refreshed.source;
}

/** Test and embedding seams. n8n calls execute() with none of these. */
export interface PlaneBloodbankDeps {
  publish?: typeof publish;
  readSecret?: SecretReader;
  fetchProjectRegistry?: RegistryFetch;
  /** Reconcile: Plane reads (default: the REST API with the node's credential). */
  planeReader?: PlaneReader;
  /** Reconcile: ticket ids that already have a creation fact (default: BLOODBANK_EVENTS). */
  knownTicketIds?: (since: Date) => Promise<Set<string>>;
  /** Reconcile: the sweep's clock. */
  now?: Date;
}

interface ConnectionOptions {
  natsHost?: string;
  natsPort?: number;
  timeoutMs?: number;
}

/** Refuse a normalized fact its schema would reject for a missing field. */
function assertRequiredData(normalized: NormalizedPlaneEvent): void {
  const schema = eventSchemas.find((candidate) => candidate.type === normalized.canonicalType);
  if (!schema) {
    throw new Error(`normalized event has no registered schema: ${normalized.canonicalType}`);
  }
  const missing = schema.dataFields
    .filter(
      (field) =>
        field.required &&
        !Object.prototype.hasOwnProperty.call(normalized.data, field.name),
    )
    .map((field) => field.name);
  if (missing.length) {
    throw new Error(
      `normalized ${normalized.canonicalType} is missing required data: ${missing.join(', ')}`,
    );
  }
}

/** The one way a Plane fact reaches the bus, webhook-born or recovered.
 *
 * The event id is derived from the normalizer's dedupe key and is also sent as
 * `Nats-Msg-Id`, so BLOODBANK_EVENTS drops a second copy of the same fact that
 * arrives inside its duplicate window. The envelope is identical whichever path
 * published it; only `data.trigger_source` says which.
 */
async function publishFact(
  send: typeof publish,
  normalized: NormalizedPlaneEvent,
  connection: ConnectionOptions,
): Promise<{ subject: string; eventId: string }> {
  assertRequiredData(normalized);
  const eventId = deterministicUuid(normalized.dedupeKey);
  const correlationId = deterministicUuid(
    `plane:${String(normalized.data.board_id)}:${String(
      normalized.data.ticket_id || normalized.data.board_id,
    )}`,
  );
  const sent = await send({
    type: normalized.canonicalType,
    data: normalized.data,
    host: connection.natsHost || undefined,
    port: connection.natsPort ? Number(connection.natsPort) : undefined,
    timeoutMs: connection.timeoutMs ? Number(connection.timeoutMs) : undefined,
    source: 'urn:33god:integration:n8n:plane-webhook',
    producer: 'n8n-plane-webhook',
    service: 'n8n',
    eventId,
    msgId: eventId,
    observedAt: normalized.observedAt,
    correlationId,
    causationId: eventId,
    orderingKey: normalized.orderingKey,
    actor: {
      type: 'ticket_provider',
      agent_id: 'bloodbank.integration.plane',
      provider: 'plane',
    },
    extensions: normalized.extensions,
  });
  return { subject: sent.subject, eventId: sent.eventId };
}

interface RoutingTable {
  routes: Map<string, PlaneProjectRoute>;
  projects: ProjectBoardsResult;
  hermesPath: string;
}

async function routingTable(
  hermesPath: string,
  projectRegistry: string | undefined,
  fetcher: RegistryFetch | undefined,
): Promise<RoutingTable> {
  const registry = await loadHermesRegistry(hermesPath);
  const hermes = planeRoutesFromRegistry(registry);
  const projects = await loadProjectBoards({ location: projectRegistry, fetcher });
  const manifests: ProjectBoard[] = [];
  for (const path of unboundRegistryProjectPaths(registry)) {
    const board = await boardFromManifest(path);
    if (board) manifests.push(board);
  }
  return { routes: mergePlaneRoutes(hermes, projects.boards, manifests), projects, hermesPath };
}

const OUTPUTS_BY_VERSION =
  '={{ $nodeVersion >= 2 ? ($parameter.operation === "reconcile" ? [{"type":"main","displayName":"Recovered"},{"type":"main","displayName":"Report"}] : [{"type":"main","displayName":"Published"},{"type":"main","displayName":"Unrouted"}]) : [{"type":"main"}] }}';

interface ReconcileSettings {
  lookbackHours?: number;
  settleMinutes?: number;
  maxPagesPerBoard?: number;
  planeBaseUrl?: string;
  rateReserve?: number;
  paceMs?: number;
  dryRun?: boolean;
}

function positive(value: unknown, fallback: number): number {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

/** Reconcile Missed Tickets: one sweep, whatever the input items are.
 *
 * Returns [recovered, report]: one item per ticket whose creation fact was
 * published (or would be, on a dry run), and one summary item for the sweep.
 */
async function reconcileMissedTickets(
  this: IExecuteFunctions,
  table: RoutingTable,
  seams: PlaneBloodbankDeps,
  send: typeof publish,
  routingJson: Record<string, unknown>,
): Promise<[INodeExecutionData[], INodeExecutionData[]]> {
  const settings = (this.getNodeParameter('reconcile', 0, {}) || {}) as ReconcileSettings;
  const connection = (this.getNodeParameter('connection', 0, {}) || {}) as ConnectionOptions;
  const baseUrl = (settings.planeBaseUrl || 'https://plane.delo.sh').replace(/\/+$/, '');
  const dryRun = settings.dryRun === true;

  // n8n passes its own argument to execute(); only a seam of the right shape counts.
  const fakePlane = seams.planeReader;
  let plane: PlaneReader | undefined =
    fakePlane && typeof fakePlane.projects === 'function' && typeof fakePlane.issuesPage === 'function'
      ? fakePlane
      : undefined;
  if (!plane) {
    const credential = (await this.getCredentials('httpHeaderAuth')) as { name?: string; value?: string };
    plane = makePlaneReader({
      baseUrl,
      header: { name: String(credential.name ?? ''), value: String(credential.value ?? '') },
      rateReserve: settings.rateReserve === undefined ? undefined : Number(settings.rateReserve),
      paceMs: settings.paceMs === undefined ? undefined : Number(settings.paceMs),
    });
  }
  const knownTicketIds = typeof seams.knownTicketIds === 'function' ? seams.knownTicketIds : ((since: Date) =>
    createdTicketIdsSince(since, {
      host: connection.natsHost || undefined,
      port: connection.natsPort ? Number(connection.natsPort) : undefined,
      timeoutMs: connection.timeoutMs ? Number(connection.timeoutMs) : undefined,
    }));

  const plan = await planReconcile({
    routes: table.routes,
    plane,
    knownTicketIds,
    now: seams.now instanceof Date ? seams.now : undefined,
    lookbackMs: positive(settings.lookbackHours, RECONCILE_DEFAULTS.lookbackMs / 3_600_000) * 3_600_000,
    settleMs: positive(settings.settleMinutes, RECONCILE_DEFAULTS.settleMs / 60_000) * 60_000,
    maxPagesPerBoard: positive(settings.maxPagesPerBoard, RECONCILE_DEFAULTS.maxPagesPerBoard),
  });

  const errored = plan.boards.filter((board) => board.status === 'error');
  if (errored.length && !plan.counts.boards_checked && !plan.partial) {
    // Nothing could be read at all (an expired key, Plane down): that is a
    // failed sweep, not an empty one.
    throw new NodeOperationError(
      this.getNode(),
      `Plane ingress reconcile read no board: ${errored[0].reason}`,
    );
  }

  const recovered: INodeExecutionData[] = [];
  const failures: Array<Record<string, unknown>> = [];
  for (const candidate of plan.candidates) {
    const payload = issueAsWebhookPayload(candidate.issue, candidate.route);
    const result = classifyPlaneWebhook(payload, table.routes);
    if (result.status !== 'routed') {
      failures.push({ ticket_id: candidate.issue.id ?? null, board_id: candidate.route.boardId, reason: result.reason });
      continue;
    }
    const normalized = result.event;
    const data = normalized.data;
    const ticketKey = (data.ticket_key as string | null) ?? null;
    const summary = {
      recovered: !dryRun,
      dry_run: dryRun,
      ticket_key: ticketKey,
      ticket_id: String(data.ticket_id),
      title: String(data.title),
      board_id: String(data.board_id),
      board_key: candidate.route.boardKey ?? null,
      repo: String(data.repo),
      workspace: String(data.workspace),
      phase: (data.phase as string | null) ?? null,
      created_at: normalized.observedAt,
      type: normalized.canonicalType,
      provider_event_type: normalized.providerEventType,
      trigger_source: String(data.trigger_source),
      url: ticketKey ? `${baseUrl}/${String(data.workspace)}/browse/${ticketKey}/` : null,
    };
    if (dryRun) {
      assertRequiredData(normalized);
      recovered.push({ json: { ok: true, ...summary, event_id: deterministicUuid(normalized.dedupeKey) }, pairedItem: { item: 0 } });
      continue;
    }
    const sent = await publishFact(send, normalized, connection);
    recovered.push({
      json: { ok: true, ...summary, event_id: sent.eventId, subject: sent.subject },
      pairedItem: { item: 0 },
    });
  }

  const report = {
    ok: true,
    operation: 'reconcile',
    dry_run: dryRun,
    window: plan.window,
    ...plan.counts,
    recovered: recovered.length,
    recovered_tickets: recovered.map((item) => item.json.ticket_key ?? item.json.ticket_id),
    partial: plan.partial,
    ...(plan.stopped_reason ? { stopped_reason: plan.stopped_reason } : {}),
    // Only the boards worth a look: skipped, errored, unchecked, truncated, or with tickets in the window.
    boards: plan.boards.filter((board) => board.status !== 'checked' || board.truncated || board.in_window > 0),
    ...(failures.length ? { failures } : {}),
    ...routingJson,
  };
  return [recovered, [{ json: report, pairedItem: { item: 0 } }]];
}

export class PlaneBloodbank implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Plane → Bloodbank',
    name: 'planeBloodbank',
    icon: { light: 'file:planeBloodbank.svg', dark: 'file:planeBloodbank.dark.svg' },
    group: ['transform'],
    version: [1, 2],
    defaultVersion: 2,
    description: 'Verify and normalize a Plane webhook, then publish one canonical Bloodbank fact',
    defaults: { name: 'Plane → Bloodbank' },
    inputs: ['main'],
    // v1 keeps its single output so a saved workflow behind a
    // "respond with last node" webhook still gets an item to answer with. v2
    // splits boards no enrolled project claims onto their own output.
    outputs: OUTPUTS_BY_VERSION as unknown as INodeTypeDescription['outputs'],
    credentials: [
      {
        // Plane's REST API key as a Header Auth credential (name X-API-Key).
        name: 'httpHeaderAuth',
        required: true,
        displayOptions: { show: { operation: ['reconcile'] } },
      },
    ],
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        default: 'webhook',
        options: [
          {
            name: 'Normalize Webhook',
            value: 'webhook',
            description: 'Verify one Plane webhook delivery and publish its canonical fact',
            action: 'Normalize a Plane webhook',
          },
          {
            name: 'Reconcile Missed Tickets',
            value: 'reconcile',
            description:
              'List tickets each routed board created in the lookback window and publish the creation fact of any the bus never received',
            action: 'Reconcile missed Plane tickets',
          },
        ],
      },
      {
        displayName:
          'Boards no enrolled project claims leave on the Unrouted output. When the webhook responds with the last node, answer Plane immediately instead (Respond: Immediately), or an unrouted delivery has no item to respond with.',
        name: 'unroutedNotice',
        type: 'notice',
        default: '',
        displayOptions: { show: { '@version': [{ _cnd: { gte: 2 } }], operation: ['webhook'] } },
      },
      {
        displayName:
          'Plane does not retry a webhook that failed with an HTTP error, so a ticket created while n8n was down never reached the bus. This finds those tickets on every routed, unarchived board and publishes their repo.task.created fact through the webhook normalizer (data.trigger_source = plane-reconcile). Recovered tickets leave on Recovered, one item each; the sweep summary leaves on Report.',
        name: 'reconcileNotice',
        type: 'notice',
        default: '',
        displayOptions: { show: { operation: ['reconcile'] } },
      },
      {
        displayName: 'Reconcile',
        name: 'reconcile',
        type: 'collection',
        placeholder: 'Add option',
        default: {},
        displayOptions: { show: { operation: ['reconcile'] } },
        options: [
          {
            displayName: 'Lookback (Hours)',
            name: 'lookbackHours',
            type: 'number',
            default: RECONCILE_DEFAULTS.lookbackMs / 3_600_000,
            description:
              'Tickets created this far back are checked. Keep it inside the grooming trigger\'s catch-up window (24 h).',
          },
          {
            displayName: 'Settle (Minutes)',
            name: 'settleMinutes',
            type: 'number',
            default: RECONCILE_DEFAULTS.settleMs / 60_000,
            description: 'Tickets younger than this are left to their webhook, which may still be in flight',
          },
          {
            displayName: 'Max Pages per Board',
            name: 'maxPagesPerBoard',
            type: 'number',
            default: RECONCILE_DEFAULTS.maxPagesPerBoard,
            description: 'Plane issue pages (100 each, newest first) read per board before giving up on the window start',
          },
          {
            displayName: 'Plane Base URL',
            name: 'planeBaseUrl',
            type: 'string',
            default: 'https://plane.delo.sh',
          },
          {
            displayName: 'Rate Limit Reserve',
            name: 'rateReserve',
            type: 'number',
            default: RECONCILE_DEFAULTS.rateReserve,
            description:
              'Stop the sweep while the API key still has this many requests left this minute, leaving them to the chip lane. The next sweep resumes on a rotated board order.',
          },
          {
            displayName: 'Pace (ms)',
            name: 'paceMs',
            type: 'number',
            default: RECONCILE_DEFAULTS.paceMs,
            description: 'Gap between Plane requests',
          },
          {
            displayName: 'Dry Run',
            name: 'dryRun',
            type: 'boolean',
            default: false,
            description: 'Whether to report what would be recovered without publishing anything',
          },
        ],
      },
      {
        displayName: 'Verify HMAC Signature',
        name: 'verifySignature',
        type: 'boolean',
        default: true,
        displayOptions: { show: { operation: ['webhook'] } },
        description: 'Whether to verify the raw webhook body before publishing anything',
      },
      {
        displayName: 'Webhook Secret References',
        name: 'webhookSecretReferences',
        type: 'json',
        default: '{}',
        required: true,
        displayOptions: { show: { verifySignature: [true], operation: ['webhook'] } },
        description:
          'JSON object mapping trusted Plane webhook IDs to op:// or env:// secret references. Raw credential values are rejected. Resolved secrets are cached in-process for an hour and served stale if 1Password is unreachable.',
      },
      {
        displayName: 'Legacy Single Secret Reference',
        name: 'secretReference',
        type: 'string',
        default: '',
        displayOptions: { show: { verifySignature: [true], operation: ['webhook'] } },
        description:
          'Backward-compatible fallback used only when the webhook secret map is empty. New workflows must use the allowlist map.',
      },
      {
        displayName: 'Routing',
        name: 'routing',
        type: 'collection',
        placeholder: 'Add option',
        default: {},
        description:
          'Boards route from pjangler project enrollment (every repo\'s .project.json), merged with the Hermes org chart',
        options: [
          {
            displayName: 'Project Registry',
            name: 'projectRegistry',
            type: 'string',
            default: '',
            placeholder: 'http://localhost:8764',
            description:
              'pjangler registry service URL or fixture file. Blank: PJ_PROJECT_REGISTRY, PJ_REGISTRY_URL, then http://localhost:8764.',
          },
        ],
      },
      {
        displayName: 'Connection',
        name: 'connection',
        type: 'collection',
        placeholder: 'Add option',
        default: {},
        options: [
          { displayName: 'NATS Host', name: 'natsHost', type: 'string', default: 'localhost' },
          { displayName: 'NATS Port', name: 'natsPort', type: 'number', default: 4222 },
          {
            displayName: 'Publish Timeout (ms)',
            name: 'timeoutMs',
            type: 'number',
            default: 3000,
          },
        ],
      },
      // Retired UI parameter, kept hidden so a saved path still applies. Blank:
      // HERMES_AGENTS_REGISTRY, HERMES_FLEET_REGISTRY_FILE, then the fleet default.
      { displayName: 'Hermes Registry File (legacy)', name: 'registryFile', type: 'hidden', default: '' },
    ],
  };

  async execute(this: IExecuteFunctions, deps?: unknown): Promise<INodeExecutionData[][]> {
    const seams = (record(deps) || {}) as PlaneBloodbankDeps;
    const send = typeof seams.publish === 'function' ? seams.publish : publish;
    const readSecret = typeof seams.readSecret === 'function' ? seams.readSecret : opRead;
    const fetcher = typeof seams.fetchProjectRegistry === 'function' ? seams.fetchProjectRegistry : undefined;
    const version = Number(this.getNode().typeVersion) || 1;

    const items = this.getInputData();
    const published: INodeExecutionData[] = [];
    const unrouted: INodeExecutionData[] = [];
    const hermesPath = hermesRegistryPath(this.getNodeParameter('registryFile', 0, ''));
    const routing = (this.getNodeParameter('routing', 0, {}) || {}) as { projectRegistry?: string };
    let table: RoutingTable;
    try {
      table = await routingTable(hermesPath, routing.projectRegistry, fetcher);
    } catch (error) {
      throw new NodeOperationError(
        this.getNode(),
        `Cannot load Plane routing registry ${hermesPath}: ${(error as Error).message}`,
      );
    }
    const routingJson = {
      routing_projects: table.projects.status,
      ...(table.projects.error ? { routing_projects_error: table.projects.error } : {}),
    };

    if (this.getNodeParameter('operation', 0, 'webhook') === 'reconcile') {
      const [recovered, report] = await reconcileMissedTickets.call(this, table, seams, send, routingJson);
      return version >= 2 ? [recovered, report] : [[...recovered, ...report]];
    }

    for (let index = 0; index < items.length; index++) {
      try {
        const input = items[index];
        const root = record(input.json) || {};
        const headers = record(root.headers) || {};
        const payload = record(root.body) || root;
        let secretSource: string | undefined;
        if (this.getNodeParameter('verifySignature', index, true) as boolean) {
          if (!input.binary?.data) {
            throw new Error('Webhook must enable Raw Body so its HMAC can be verified');
          }
          const rawBody = await this.helpers.getBinaryDataBuffer(index, 'data');
          const references = parseWebhookSecretReferences(
            this.getNodeParameter('webhookSecretReferences', index, {}),
          );
          const reference = secretReferenceForWebhook(
            payload,
            references,
            String(this.getNodeParameter('secretReference', index, '')),
          );
          secretSource = await verifyWithSecret(reference, rawBody, headers, readSecret);
        }

        const secretJson = secretSource ? { secret_source: secretSource } : {};
        const result = classifyPlaneWebhook(payload, table.routes);
        if (result.status === 'unsupported') {
          published.push({
            json: { ok: true, routed: false, unsupported: true, reason: result.reason, ...secretJson, ...routingJson },
            pairedItem: { item: index },
          });
          continue;
        }
        if (result.status === 'unrouted') {
          const json = {
            ok: true,
            routed: false,
            unrouted: true,
            reason: result.reason,
            board_id: result.boardId,
            workspace: result.workspace ?? null,
            plane_event: `${result.providerEvent}.${result.action}`,
            webhook_id: typeof payload.webhook_id === 'string' ? payload.webhook_id : null,
            ...secretJson,
            ...routingJson,
          };
          // v1 answers on its only output, exactly as before; v2 surfaces it.
          (version >= 2 ? unrouted : published).push({ json, pairedItem: { item: index } });
          continue;
        }

        const normalized = result.event;
        const connection = this.getNodeParameter('connection', index, {}) as ConnectionOptions;
        const sent = await publishFact(send, normalized, connection);
        published.push({
          json: {
            ok: true,
            routed: true,
            type: normalized.canonicalType,
            provider_event_type: normalized.providerEventType,
            subject: sent.subject,
            event_id: sent.eventId,
            board_id: String(normalized.data.board_id),
            repo: (normalized.data.repo as string | null) ?? null,
            slug: String(normalized.data.slug),
            workspace: String(normalized.data.workspace),
            route_source: result.route?.source ?? null,
            ...secretJson,
            ...routingJson,
          },
          pairedItem: { item: index },
        });
      } catch (error) {
        if (this.continueOnFail()) {
          published.push({
            json: { ok: false, routed: false, error: (error as Error).message },
            pairedItem: { item: index },
          });
          continue;
        }
        throw new NodeOperationError(this.getNode(), (error as Error).message, {
          itemIndex: index,
        });
      }
    }
    return version >= 2 ? [published, unrouted] : [published];
  }
}
