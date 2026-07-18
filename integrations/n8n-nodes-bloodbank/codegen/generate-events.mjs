#!/usr/bin/env node
// Generate src/nodes/Bloodbank/eventSchemas.ts from the intersection of
// bloodbank/schemas/bloodbank/v1/** and publisher-events.json. Schema existence
// is not producer authorization; add/change both contracts intentionally.
import { readdirSync, readFileSync, writeFileSync, statSync, mkdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
// package: bloodbank/integrations/n8n-nodes-bloodbank/ ; schemas: bloodbank/schemas/bloodbank/v1
const schemasRoot = resolve(here, '..', '..', '..', 'schemas', 'bloodbank', 'v1');
const outFile = resolve(here, '..', 'src', 'nodes', 'Bloodbank', 'eventSchemas.ts');
const repoTaskContractsFile = resolve(here, '..', 'src', 'repoTaskContracts.ts');
const publisherPolicyFile = resolve(here, '..', 'publisher-events.json');
const publisherPolicy = JSON.parse(readFileSync(publisherPolicyFile, 'utf8'));
const authorizedEvents = new Set(publisherPolicy.events || []);
const repoTaskSourceTimestampFields = new Map([
  ['bloodbank.v1.repo.task.created', null],
  ['bloodbank.v1.repo.task.recorded', 'updated_at'],
  ['bloodbank.v1.repo.task.completed', 'completed_at'],
]);

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
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

const supportedRepoTaskDataKeywords = new Set([
  'type',
  'description',
  'properties',
  'required',
  'additionalProperties',
]);
const supportedRepoTaskFieldKeywords = new Set([
  'type',
  'description',
  'minLength',
  'enum',
  'format',
  'pattern',
]);

function repoTaskFieldContract(type, name, definition) {
  const unsupported = Object.keys(definition).filter(
    (keyword) => !supportedRepoTaskFieldKeywords.has(keyword),
  );
  if (unsupported.length) {
    throw new Error(
      `${type} data.${name} uses unsupported runtime validation keyword(s): ${unsupported.join(', ')}`,
    );
  }
  if (definition.type === undefined) {
    throw new Error(`${type} data.${name} must declare an explicit JSON type`);
  }
  if (definition.format !== undefined && definition.format !== 'date-time') {
    throw new Error(`${type} data.${name} uses unsupported format ${definition.format}`);
  }
  if (definition.pattern !== undefined) new RegExp(definition.pattern);
  return {
    types: Array.isArray(definition.type) ? definition.type : [definition.type],
    ...(typeof definition.minLength === 'number'
      ? { minLength: definition.minLength }
      : {}),
    ...(Array.isArray(definition.enum) ? { enumValues: definition.enum } : {}),
    ...(typeof definition.format === 'string' ? { format: definition.format } : {}),
    ...(typeof definition.pattern === 'string' ? { pattern: definition.pattern } : {}),
  };
}

const events = [];
const repoTaskContracts = {};
for (const file of walk(schemasRoot)) {
  let schema;
  try {
    schema = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    continue;
  }
  const props = schema.properties || {};
  const type = props.type && props.type.const;
  if (!type) continue;
  const kind = props.kind && props.kind.const;
  if (kind && kind !== 'event') continue; // publish events only, not commands/replies
  if (!authorizedEvents.has(type)) continue; // a schema is not producer authorization
  const domain = (props.domain && props.domain.const) || type.split('.')[2];
  const dataSchema = props.data || {};
  const dataProps = dataSchema.properties || {};
  const dataRequired = dataSchema.required || [];
  const dataFields = Object.entries(dataProps).map(([name, def]) => ({
    name,
    jsonType: scalarType(def.type),
    required: dataRequired.includes(name),
    description: (def.description || '').replace(/\s+/g, ' ').trim().slice(0, 200),
  }));
  events.push({
    type,
    domain,
    title: schema.title || type,
    description: (schema.description || '').replace(/\s+/g, ' ').trim().slice(0, 240),
    dataFields,
  });
  if (repoTaskSourceTimestampFields.has(type)) {
    const unsupportedDataKeywords = Object.keys(dataSchema).filter(
      (keyword) => !supportedRepoTaskDataKeywords.has(keyword),
    );
    if (unsupportedDataKeywords.length) {
      throw new Error(
        `${type} data uses unsupported runtime validation keyword(s): ${unsupportedDataKeywords.join(', ')}`,
      );
    }
    if (dataSchema.type !== 'object') {
      throw new Error(`${type} data must be a JSON object schema`);
    }
    if (
      dataSchema.additionalProperties !== undefined &&
      typeof dataSchema.additionalProperties !== 'boolean'
    ) {
      throw new Error(`${type} data uses an unsupported additionalProperties schema`);
    }
    const sourceTimestampField = repoTaskSourceTimestampFields.get(type);
    if (sourceTimestampField) {
      const timestampSchema = dataProps[sourceTimestampField];
      if (!timestampSchema || timestampSchema.type !== 'string' || timestampSchema.format !== 'date-time') {
        throw new Error(
          `${type} canonical source timestamp ${sourceTimestampField} must be a string/date-time field`,
        );
      }
    }
    repoTaskContracts[type] = {
      sourceTimestampField,
      additionalProperties: dataSchema.additionalProperties !== false,
      required: dataRequired,
      fields: Object.fromEntries(
        Object.entries(dataProps).map(([name, def]) => [
          name,
          repoTaskFieldContract(type, name, def),
        ]),
      ),
    };
  }
}
events.sort((a, b) => a.type.localeCompare(b.type));

const generatedTypes = new Set(events.map((event) => event.type));
const missingAuthorized = [...authorizedEvents].filter((type) => !generatedTypes.has(type));
if (missingAuthorized.length) {
  throw new Error(
    `publisher-events.json authorizes missing/non-event schema(s): ${missingAuthorized.join(', ')}`,
  );
}
const unconfiguredAuthorizedRepoTasks = [...authorizedEvents].filter(
  (type) => type.startsWith('bloodbank.v1.repo.task.') && !repoTaskSourceTimestampFields.has(type),
);
if (unconfiguredAuthorizedRepoTasks.length) {
  throw new Error(
    `authorized repo-task source(s) lack runtime contracts: ${unconfiguredAuthorizedRepoTasks.join(', ')}`,
  );
}
const missingRepoTaskContracts = [...repoTaskSourceTimestampFields.keys()].filter(
  (type) => !repoTaskContracts[type],
);
if (missingRepoTaskContracts.length) {
  throw new Error(
    `missing authorized repo-task contract(s): ${missingRepoTaskContracts.join(', ')}`,
  );
}

const banner =
  '// AUTO-GENERATED by codegen/generate-events.mjs from Bloodbank schemas + publisher-events.json.\n' +
  '// Do not edit by hand — run `npm run codegen`.\n\n';
const iface =
  'export interface EventDataField {\n' +
  '  name: string;\n  jsonType: string;\n  required: boolean;\n  description: string;\n}\n\n' +
  'export interface EventSchema {\n' +
  '  type: string;\n  domain: string;\n  title: string;\n  description: string;\n  dataFields: EventDataField[];\n}\n\n';
const body = 'export const eventSchemas: EventSchema[] = ' + JSON.stringify(events, null, 2) + ';\n';

const repoTaskBanner =
  '// AUTO-GENERATED by codegen/generate-events.mjs from the authorized repo.task JSON Schemas.\n' +
  '// Do not edit by hand — run `npm run codegen`. These are the only n8n events with\n' +
  '// production field-level validation; other authorized events retain legacy checks.\n\n';
const repoTaskInterfaces =
  "export type JsonValueType = 'array' | 'boolean' | 'integer' | 'null' | 'number' | 'object' | 'string';\n\n" +
  'export interface RepoTaskFieldContract {\n' +
  '  types: JsonValueType[];\n' +
  '  minLength?: number;\n' +
  '  enumValues?: unknown[];\n' +
  '  format?: string;\n' +
  '  pattern?: string;\n' +
  '}\n\n' +
  'export interface RepoTaskContract {\n' +
  '  sourceTimestampField: string | null;\n' +
  '  additionalProperties: boolean;\n' +
  '  required: string[];\n' +
  '  fields: Record<string, RepoTaskFieldContract>;\n' +
  '}\n\n';
const repoTaskBody =
  'export const repoTaskContracts: Record<string, RepoTaskContract> = ' +
  JSON.stringify(repoTaskContracts, null, 2) +
  ';\n';

mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, banner + iface + body);
writeFileSync(repoTaskContractsFile, repoTaskBanner + repoTaskInterfaces + repoTaskBody);
console.log(
  `generated ${events.length} authorized events and ${Object.keys(repoTaskContracts).length} repo-task contracts`,
);
