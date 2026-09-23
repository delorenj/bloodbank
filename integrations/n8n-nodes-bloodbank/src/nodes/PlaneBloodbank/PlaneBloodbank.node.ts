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
  mergePlaneRoutes,
  planeRoutesFromRegistry,
  unboundRegistryProjectPaths,
} from '../../plane';
import type { PlaneProjectRoute } from '../../plane';
import { boardFromManifest, loadProjectBoards } from '../../projects';
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
  '={{ $nodeVersion >= 2 ? [{"type":"main","displayName":"Published"},{"type":"main","displayName":"Unrouted"}] : [{"type":"main"}] }}';

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
    properties: [
      {
        displayName:
          'Boards no enrolled project claims leave on the Unrouted output. When the webhook responds with the last node, answer Plane immediately instead (Respond: Immediately), or an unrouted delivery has no item to respond with.',
        name: 'unroutedNotice',
        type: 'notice',
        default: '',
        displayOptions: { show: { '@version': [{ _cnd: { gte: 2 } }] } },
      },
      {
        displayName: 'Verify HMAC Signature',
        name: 'verifySignature',
        type: 'boolean',
        default: true,
        description: 'Whether to verify the raw webhook body before publishing anything',
      },
      {
        displayName: 'Webhook Secret References',
        name: 'webhookSecretReferences',
        type: 'json',
        default: '{}',
        required: true,
        displayOptions: { show: { verifySignature: [true] } },
        description:
          'JSON object mapping trusted Plane webhook IDs to op:// or env:// secret references. Raw credential values are rejected. Resolved secrets are cached in-process for an hour and served stale if 1Password is unreachable.',
      },
      {
        displayName: 'Legacy Single Secret Reference',
        name: 'secretReference',
        type: 'string',
        default: '',
        displayOptions: { show: { verifySignature: [true] } },
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
        const connection = this.getNodeParameter('connection', index, {}) as {
          natsHost?: string;
          natsPort?: number;
          timeoutMs?: number;
        };
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
