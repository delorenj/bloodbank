import { canonicalSchemaDocuments } from './nodes/Bloodbank/eventSchemas';

export const CANDYBAR_CREATED = 'bloodbank.cron.candybar.created';
export const CANDYBAR_DELETED = 'bloodbank.cron.candybar.deleted';
type Document = Record<string, any>;

/** Exact local branch selection, not a general revision framework. */
export function candybarRevision(document: Document, revision: unknown): Document {
  if (document.properties?.type?.const !== CANDYBAR_CREATED) return document;
  if (revision !== 1 && revision !== 2) {
    throw new Error(`unsupported ${CANDYBAR_CREATED} schema_version ${String(revision)}`);
  }
  const branch = document.oneOf?.find(
    (candidate: Document) => candidate.properties?.data?.properties?.schema_version?.const === revision,
  );
  if (!branch) throw new Error(`missing canonical candybar revision ${revision}`);
  return {
    ...document, oneOf: undefined, allOf: branch.allOf,
    properties: { ...document.properties, ...branch.properties },
  };
}

export function candybarHandles(type: string, data: Record<string, unknown>): {
  dataschema: string; schemaref: string;
} | undefined {
  if (type !== CANDYBAR_CREATED && type !== CANDYBAR_DELETED) return undefined;
  const document = canonicalSchemaDocuments.find(
    (candidate: Document) => candidate.properties?.type?.const === type,
  );
  if (!document) throw new Error(`missing canonical schema for ${type}`);
  if (type === CANDYBAR_DELETED && data.schema_version !== 1) {
    throw new Error(`unsupported ${CANDYBAR_DELETED} schema_version ${String(data.schema_version)}`);
  }
  const selected = candybarRevision(document, data.schema_version);
  return {
    dataschema: selected.properties.dataschema.const,
    schemaref: selected.properties.schemaref.const,
  };
}

/** Shape validation alone cannot enforce finite wall-time/zone semantics.
 * Keep this consumer fail-closed until it has canonical semantic parity.
 * Building an envelope for inspection is allowed; publishing v2 is not.
 */
export function assertCandybarConsumer(envelope: Record<string, unknown>): void {
  if (envelope.type !== CANDYBAR_CREATED && envelope.type !== CANDYBAR_DELETED) return;
  const data = envelope.data as Record<string, unknown>;
  if (envelope.kind !== 'event') throw new Error('candybar created/deleted must be events');
  if (envelope.ordering_key !== `candybar:${data.candybar_id}`) {
    throw new Error('ordering_key must equal candybar:<data.candybar_id> exactly');
  }
  if (envelope.type === CANDYBAR_CREATED && data.schema_version === 2) {
    throw new Error(
      'n8n finite candybar v2 publication/semantic validation is unsupported; '
      + 'use the canonical Python assert_contract + validate_envelope path for calendar and IANA validation',
    );
  }
}
