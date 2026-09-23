#!/usr/bin/env node
// Generate src/nodes/Bloodbank/eventSchemas.ts from the canonical schema tree.
// This is what makes both publisher and consumer selections schema-backed: add
// or change a schema, run `npm run codegen` (or `npm run deploy`), and the event
// and command dropdowns update together.
//
// Besides the raw schema documents it emits, per schema:
//   label / group     the human dropdown name ("Repo" · "On Task Created"),
//                     overridable with a root-level "x-n8n-label"
//   description       the schema description minus naming-contract boilerplate
//   providerAliases   every data.provider_event_type "x-provider-aliases"
//                     entry, with the canonical type it filters. The schema is
//                     the single source for provider aliases such as
//                     plane.ticket.created; nothing else may declare one.
import { readdirSync, readFileSync, writeFileSync, statSync, mkdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
// package: bloodbank/integrations/n8n-nodes-bloodbank/ ; schemas: bloodbank/schemas
const schemasRoot = resolve(here, '..', '..', '..', 'schemas');
const eventSchemasRoot = join(schemasRoot, 'bloodbank');
const outFile = resolve(here, '..', 'src', 'nodes', 'Bloodbank', 'eventSchemas.ts');

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (name.endsWith('.json')) out.push(p);
  }
  return out;
}

function scalarType(t) {
  if (Array.isArray(t)) t = t.find((x) => x !== 'null') || 'string';
  return t || 'string';
}

// Tokens that read wrong in Title Case. A label is for a human scanning a long
// dropdown: "Cli Session Started" and "Llm Request Sent" look like typos.
const ACRONYMS = {
  api: 'API',
  ci: 'CI',
  cli: 'CLI',
  id: 'ID',
  llm: 'LLM',
  mcp: 'MCP',
  pm: 'PM',
  pr: 'PR',
  tts: 'TTS',
  url: 'URL',
};

function titleWord(word) {
  const lower = word.toLowerCase();
  if (ACRONYMS[lower]) return ACRONYMS[lower];
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

function titleCase(token) {
  return String(token)
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map(titleWord)
    .join(' ');
}

// Every other schema description ends by pointing at the naming contract. That
// sentence is true and useless in a dropdown, where it eats the space the real
// description needs.
function cleanDescription(text) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .replace(/\s*See (?:bloodbank\/)?docs\/event-naming\.md(?:\s*§\s*[\d.]+)?\.?/gi, '')
    .replace(/\s+per (?:bloodbank\/)?docs\/event-naming\.md/gi, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([.;,])/g, '$1')
    .trim();
}

function derivedLabel(kind, entity, action) {
  const entityText = titleCase(entity);
  const actionText = titleCase(action);
  // clock.clock_in reads "Clock In", not "Clock In Clock".
  const redundant = actionText.toLowerCase().startsWith(entityText.toLowerCase());
  if (kind === 'command') return redundant ? actionText : `${actionText} ${entityText}`;
  return redundant ? `On ${actionText}` : `On ${entityText} ${actionText}`;
}

const canonicalSchemaDocuments = [];
for (const file of walk(schemasRoot)) {
  let schema;
  try {
    schema = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    continue;
  }
  if (typeof schema.$id === 'string' && schema.$id) canonicalSchemaDocuments.push(schema);
}
canonicalSchemaDocuments.sort((a, b) => a.$id.localeCompare(b.$id));

const schemas = [];
const providerAliases = [];
const aliasValues = new Map();
for (const file of walk(eventSchemasRoot)) {
  let schema;
  try {
    schema = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    continue;
  }
  const props = schema.properties || {};
  const type = props.type && props.type.const;
  if (!type) continue;
  const kind = (props.kind && (props.kind.const || (props.kind.enum?.includes('command') ? 'command' : undefined))) || 'event';
  if (kind !== 'event' && kind !== 'command') continue;
  const [, typeDomain, entity, action] = type.split('.');
  const domain = (props.domain && props.domain.const) || typeDomain;
  const dataProps = (props.data && props.data.properties) || {};
  const dataRequired = (props.data && props.data.required) || [];
  const dataFields = Object.entries(dataProps).map(([name, def]) => ({
    name,
    jsonType: scalarType(def.type),
    required: dataRequired.includes(name),
    description: (def.description || '').replace(/\s+/g, ' ').trim().slice(0, 200),
  }));
  const label = typeof schema['x-n8n-label'] === 'string' && schema['x-n8n-label'].trim()
    ? schema['x-n8n-label'].trim()
    : derivedLabel(kind, entity, action);
  const group = titleCase(domain);

  const declared = dataProps.provider_event_type && dataProps.provider_event_type['x-provider-aliases'];
  const aliases = [];
  for (const alias of Array.isArray(declared) ? declared : []) {
    const value = String(alias?.value || '').trim();
    const provider = String(alias?.provider || '').trim();
    if (!value || !provider) {
      throw new Error(`${file}: every x-provider-aliases entry needs a value and a provider`);
    }
    if (!value.startsWith(`${provider}.`)) {
      throw new Error(`${file}: provider alias ${value} must start with its provider "${provider}."`);
    }
    if (value.startsWith('bloodbank.')) {
      throw new Error(`${file}: provider alias ${value} must not look like a canonical type`);
    }
    if (aliasValues.has(value)) {
      throw new Error(`${file}: provider alias ${value} is already declared by ${aliasValues.get(value)}`);
    }
    aliasValues.set(value, type);
    const entry = {
      value,
      provider,
      label: String(alias.label || '').trim() || `On ${titleCase(value.split('.').slice(1).join(' '))}`,
      description: cleanDescription(alias.description),
      canonicalType: type,
    };
    aliases.push(entry);
    providerAliases.push(entry);
  }

  schemas.push({
    type,
    kind,
    domain,
    schemaId: schema.$id,
    title: schema.title || type,
    label,
    group,
    description: cleanDescription(schema.description).slice(0, 240),
    dataFields,
    providerAliases: aliases,
  });
}
schemas.sort((a, b) => a.type.localeCompare(b.type));
providerAliases.sort((a, b) =>
  a.canonicalType.localeCompare(b.canonicalType) || a.value.localeCompare(b.value),
);

const banner =
  '// AUTO-GENERATED by codegen/generate-events.mjs from bloodbank/schemas/**.\n' +
  '// Do not edit by hand — run `npm run codegen`.\n\n';
const iface =
  'export interface EventDataField {\n' +
  '  name: string;\n  jsonType: string;\n  required: boolean;\n  description: string;\n}\n\n' +
  'export interface ProviderAlias {\n' +
  '  /** Provider provenance name carried in data.provider_event_type, e.g. plane.ticket.created. */\n' +
  '  value: string;\n  provider: string;\n  label: string;\n  description: string;\n' +
  '  /** The canonical Bloodbank type whose subject this alias filters. */\n' +
  '  canonicalType: string;\n}\n\n' +
  'export interface EventSchema {\n' +
  "  type: string;\n  kind: 'event' | 'command';\n  domain: string;\n  schemaId: string;\n  title: string;\n" +
  '  label: string;\n  group: string;\n  description: string;\n  dataFields: EventDataField[];\n' +
  '  providerAliases: ProviderAlias[];\n}\n\n';
const body =
  'export type CanonicalSchemaDocument = Record<string, unknown>;\n\n' +
  'export const canonicalSchemaDocuments: CanonicalSchemaDocument[] = ' +
  JSON.stringify(canonicalSchemaDocuments, null, 2) + ';\n\n' +
  'const schemas: EventSchema[] = ' + JSON.stringify(schemas, null, 2) + ';\n\n' +
  "export const eventSchemas = schemas.filter((schema) => schema.kind === 'event');\n" +
  "export const commandSchemas = schemas.filter((schema) => schema.kind === 'command');\n\n" +
  '/** Every provider alias declared in the schema tree, ordered by canonical type. */\n' +
  'export const providerAliases: ProviderAlias[] = ' + JSON.stringify(providerAliases, null, 2) + ';\n';

mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, banner + iface + body);
const eventCount = schemas.filter((schema) => schema.kind === 'event').length;
const commandCount = schemas.filter((schema) => schema.kind === 'command').length;
console.log(
  `generated ${eventCount} events + ${commandCount} commands + ${providerAliases.length} provider aliases -> ${outFile}`,
);
