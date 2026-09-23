import type { INodePropertyOptions } from 'n8n-workflow';

import {
  commandSchemas,
  eventSchemas,
  providerAliases,
} from './nodes/Bloodbank/eventSchemas';
import type { EventSchema, ProviderAlias } from './nodes/Bloodbank/eventSchemas';

/** The one option shape every Bloodbank dropdown renders:
 *  `<Group> · <Label> (<value>)`, e.g. `Repo · On Task Created (bloodbank.repo.task.created)`.
 *
 * The value in parentheses is what a workflow stores, so a reader can always
 * match what they see in the canvas to what they would grep for on the bus.
 */
export function optionName(group: string, label: string, value: string): string {
  return `${group} · ${label} (${value})`;
}

/** `Repo · On Task Created`: the human half of an option name. */
export function schemaDisplayName(schema: Pick<EventSchema, 'group' | 'label'>): string {
  return `${schema.group} · ${schema.label}`;
}

function providerTitle(provider: string): string {
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

function requiredNote(schema: EventSchema): string {
  const required = schema.dataFields.filter((field) => field.required).map((field) => field.name);
  return required.length ? ` Data requires: ${required.join(', ')}.` : '';
}

export interface SchemaOptionSettings {
  /** Append the schema's required data fields to the description (publisher). */
  withRequired?: boolean;
}

export function schemaOption(
  schema: EventSchema,
  settings: SchemaOptionSettings = {},
): INodePropertyOptions {
  const base = schema.description || schema.title;
  return {
    name: optionName(schema.group, schema.label, schema.type),
    value: schema.type,
    description: settings.withRequired ? `${base}${requiredNote(schema)}` : base,
  };
}

/** A provider alias renders in the same shape, grouped under its provider. */
export function aliasOption(alias: ProviderAlias): INodePropertyOptions {
  const canonical = eventSchemas.find((schema) => schema.type === alias.canonicalType);
  const target = canonical ? schemaDisplayName(canonical) : alias.canonicalType;
  const detail = alias.description ? ` — ${alias.description}` : '';
  return {
    name: optionName(providerTitle(alias.provider), alias.label, alias.value),
    value: alias.value,
    description: `Filters ${target} to provider=${alias.provider}${detail}`,
  };
}

export interface EventOptionSettings extends SchemaOptionSettings {
  /** List each schema's provider aliases immediately after it. */
  includeAliases?: boolean;
}

/** Every canonical event, each followed by the provider aliases that filter it. */
export function eventOptions(settings: EventOptionSettings = {}): INodePropertyOptions[] {
  const options: INodePropertyOptions[] = [];
  for (const schema of eventSchemas) {
    options.push(schemaOption(schema, settings));
    if (settings.includeAliases) {
      for (const alias of schema.providerAliases) options.push(aliasOption(alias));
    }
  }
  return options;
}

export function commandOptions(
  filter: (schema: EventSchema) => boolean = () => true,
  settings: SchemaOptionSettings = {},
): INodePropertyOptions[] {
  return commandSchemas.filter(filter).map((schema) => schemaOption(schema, settings));
}

/** Provider aliases only, e.g. for a provider-event guard. */
export function providerAliasOptions(provider?: string): INodePropertyOptions[] {
  return providerAliases
    .filter((alias) => !provider || alias.provider === provider)
    .map(aliasOption);
}

export function aliasFor(value: string): ProviderAlias | undefined {
  return providerAliases.find((alias) => alias.value === value);
}
