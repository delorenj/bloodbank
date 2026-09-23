import { randomUUID } from 'node:crypto';

import { buildEnvelope, subjectFor } from './nats';
import {
  canonicalSchemaDocuments,
  commandSchemas,
  eventSchemas,
} from './nodes/Bloodbank/eventSchemas';
import { aliasFor } from './options';

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** The canonical type a trigger binding subscribes to. */
export function canonicalTypeFor(binding: string): string {
  if (binding.startsWith('bloodbank.')) return binding;
  const alias = aliasFor(binding);
  if (!alias) throw new Error(`unknown Bloodbank event binding: ${binding}`);
  return alias.canonicalType;
}

export function bindingSubject(binding: string, kind: 'event' | 'command'): string {
  return subjectFor(canonicalTypeFor(binding), kind);
}

/** Does one envelope satisfy one binding?
 *
 * A canonical binding matches on `type`. A provider alias subscribes to its
 * canonical subject and additionally requires `data.provider` and
 * `data.provider_event_type` to name it — one subject, many filters.
 */
export function bindingMatches(binding: string, envelope: Record<string, unknown>): boolean {
  if (binding.startsWith('bloodbank.')) return envelope.type === binding;
  const alias = aliasFor(binding);
  if (!alias || envelope.type !== alias.canonicalType) return false;
  const data = record(envelope.data) || {};
  return data.provider === alias.provider && data.provider_event_type === alias.value;
}

// ---------------------------------------------------------------------------
// Generated samples
// ---------------------------------------------------------------------------

const MAX_DEPTH = 5;

function resolveRef(ref: string): Record<string, unknown> | null {
  const [file, pointer = ''] = ref.split('#');
  const base = file.split('/').pop() || '';
  const document = base
    ? canonicalSchemaDocuments.find((candidate) =>
        String(candidate.$id || '').endsWith(`/${base}`),
      )
    : undefined;
  if (!document) return null;
  let current: unknown = document;
  for (const token of pointer.split('/').filter(Boolean)) {
    const container = record(current);
    if (!container) return null;
    current = container[token.replace(/~1/g, '/').replace(/~0/g, '~')];
  }
  return record(current);
}

function sampleString(name: string, schema: Record<string, unknown>): string {
  const format = String(schema.format || '');
  if (format === 'date-time') return new Date().toISOString();
  if (format === 'date') return new Date().toISOString().slice(0, 10);
  if (format === 'uuid' || /^\^\[0-9a-f\]\{8\}/i.test(String(schema.pattern || ''))) return randomUUID();
  if (format === 'uri' || format === 'uri-reference') return `urn:33god:sample:${name || 'value'}`;
  return `sample-${name || 'value'}`.replace(/_/g, '-');
}

function sampleValue(schemaValue: unknown, name: string, depth: number): unknown {
  let schema = record(schemaValue) || {};
  if (typeof schema.$ref === 'string') {
    schema = { ...(resolveRef(schema.$ref) || {}), ...schema, $ref: undefined };
  }
  if (schema.const !== undefined) return schema.const;
  if (Array.isArray(schema.examples) && schema.examples.length) return schema.examples[0];
  if (schema.default !== undefined) return schema.default;
  if (Array.isArray(schema.enum)) {
    const choice = schema.enum.find((entry) => entry !== null);
    return choice === undefined ? null : choice;
  }
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  const type = types.find((entry) => entry && entry !== 'null') || (schema.properties ? 'object' : 'string');
  switch (type) {
    case 'object': {
      if (depth >= MAX_DEPTH) return {};
      const out: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(record(schema.properties) || {})) {
        out[key] = sampleValue(child, key, depth + 1);
      }
      return out;
    }
    case 'array': {
      const min = Number(schema.minItems) || 0;
      if (!min || depth >= MAX_DEPTH) return [];
      return Array.from({ length: min }, () => sampleValue(schema.items, name, depth + 1));
    }
    case 'integer':
    case 'number':
      return Number.isFinite(Number(schema.minimum)) ? Number(schema.minimum) : 0;
    case 'boolean':
      return false;
    default:
      return sampleString(name, schema);
  }
}

/** A schema-shaped envelope for a binding, marked as a sample.
 *
 * Used when a manual test has nothing real to replay. Every data property the
 * schema declares is present, so a workflow author can map fields against the
 * real shape; a provider alias also gets its provider provenance filled in, so
 * it passes the alias filter it was generated for.
 */
export function sampleEnvelope(binding: string, kind: 'event' | 'command'): Record<string, unknown> {
  const type = canonicalTypeFor(binding);
  const schema = [...eventSchemas, ...commandSchemas].find((candidate) => candidate.type === type);
  const document = schema
    ? canonicalSchemaDocuments.find((candidate) => candidate.$id === schema.schemaId)
    : undefined;
  const dataSchema = record(record(document?.properties)?.data);
  const data = (record(sampleValue(dataSchema, 'data', 0)) || {}) as Record<string, unknown>;
  const alias = aliasFor(binding);
  if (alias) {
    data.provider = alias.provider;
    data.provider_event_type = alias.value;
  }
  const { envelope } = buildEnvelope({
    type,
    kind,
    data,
    source: 'urn:33god:service:n8n-bloodbank-trigger',
    producer: 'n8n',
    service: 'n8n-sample',
    extensions: { sample: true },
  });
  return envelope;
}
