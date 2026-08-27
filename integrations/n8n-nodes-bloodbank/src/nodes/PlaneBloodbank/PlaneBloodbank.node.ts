import { execFile as execFileCallback } from 'node:child_process';
import { createHmac, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { promisify } from 'node:util';

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';
import { parse as parseYaml } from 'yaml';

import { deterministicUuid, publish } from '../../nats';
import { normalizePlaneWebhook, planeRoutesFromRegistry } from '../../plane';
import { eventSchemas } from '../Bloodbank/eventSchemas';

const execFile = promisify(execFileCallback);

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

function expandHome(path: string): string {
  if (path === '~') return homedir();
  if (path.startsWith('~/')) return `${homedir()}/${path.slice(2)}`;
  return path;
}

async function resolveSecret(reference: string): Promise<string> {
  if (reference.startsWith('env://')) {
    const variable = reference.slice('env://'.length);
    if (!/^[A-Z][A-Z0-9_]*$/.test(variable)) {
      throw new Error('env:// secret references must name an uppercase environment variable');
    }
    const value = process.env[variable];
    if (!value) throw new Error(`secret environment variable ${variable} is not set`);
    return value.startsWith('op://') ? resolveSecret(value) : value;
  }
  if (reference.startsWith('op://')) {
    const { stdout } = await execFile('op', ['read', reference], {
      encoding: 'utf8',
      timeout: 5000,
      maxBuffer: 16 * 1024,
    });
    const value = stdout.trim();
    if (!value) throw new Error('1Password returned an empty webhook secret');
    return value;
  }
  throw new Error('Webhook Secret Reference must use op:// or env://; raw secrets are forbidden');
}

function headerValue(headers: Record<string, unknown>, name: string): string | undefined {
  const value = headers[name] ?? headers[name.toLowerCase()] ?? headers[name.toUpperCase()];
  if (Array.isArray(value)) return value.length ? String(value[0]) : undefined;
  return typeof value === 'string' && value ? value : undefined;
}

function verifyHmac(rawBody: Buffer, headers: Record<string, unknown>, secret: string): void {
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
    throw new Error('Plane HMAC signature mismatch');
  }
}

export class PlaneBloodbank implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Plane → Bloodbank',
    name: 'planeBloodbank',
    icon: { light: 'file:planeBloodbank.svg', dark: 'file:planeBloodbank.dark.svg' },
    group: ['transform'],
    version: 1,
    description: 'Verify and normalize a Plane webhook, then publish one canonical Bloodbank fact',
    defaults: { name: 'Plane → Bloodbank' },
    inputs: ['main'],
    outputs: ['main'],
    properties: [
      {
        displayName: 'Verify HMAC Signature',
        name: 'verifySignature',
        type: 'boolean',
        default: true,
        description: 'Verify the raw webhook body before publishing anything',
      },
      {
        displayName: 'Webhook Secret References',
        name: 'webhookSecretReferences',
        type: 'json',
        default: '{}',
        required: true,
        displayOptions: { show: { verifySignature: [true] } },
        description:
          'JSON object mapping trusted Plane webhook IDs to op:// or env:// secret references. Raw credential values are rejected.',
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
        displayName: 'Hermes Registry File',
        name: 'registryFile',
        type: 'string',
        default: '~/.hermes/agents-registry.yaml',
        required: true,
        description: 'Maps Plane board IDs to canonical repo slugs and workspace metadata.',
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
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const output: INodeExecutionData[] = [];
    const registryPath = expandHome(String(this.getNodeParameter('registryFile', 0)));
    let routes;
    try {
      routes = planeRoutesFromRegistry(parseYaml(await readFile(registryPath, 'utf8')));
    } catch (error) {
      throw new NodeOperationError(
        this.getNode(),
        `Cannot load Plane routing registry ${registryPath}: ${(error as Error).message}`,
      );
    }

    for (let index = 0; index < items.length; index++) {
      try {
        const input = items[index];
        const root = record(input.json) || {};
        const headers = record(root.headers) || {};
        const payload = record(root.body) || root;
        if (this.getNodeParameter('verifySignature', index) as boolean) {
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
          const secret = await resolveSecret(reference);
          verifyHmac(rawBody, headers, secret);
        }

        const normalized = normalizePlaneWebhook(payload, routes);
        if (!normalized) {
          output.push({
            json: { ok: true, routed: false, reason: 'unsupported event or unmapped board' },
            pairedItem: { item: index },
          });
          continue;
        }
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
        const result = await publish({
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
        output.push({
          json: {
            ok: true,
            routed: true,
            type: normalized.canonicalType,
            provider_event_type: normalized.providerEventType,
            subject: result.subject,
            event_id: result.eventId,
            board_id: String(normalized.data.board_id),
            slug: String(normalized.data.slug),
            workspace: String(normalized.data.workspace),
          },
          pairedItem: { item: index },
        });
      } catch (error) {
        if (this.continueOnFail()) {
          output.push({
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
    return [output];
  }
}
