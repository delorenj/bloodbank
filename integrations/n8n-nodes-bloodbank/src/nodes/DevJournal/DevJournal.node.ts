import type { IDataObject, IExecuteFunctions, INodeExecutionData, INodeType, INodeTypeDescription } from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';
import { journalPrograms } from './programs.generated';
import { journalStore } from './JournalStore';

type JournalOperation =
  | 'ingest' | 'selectReport' | 'processReport' | 'prepareEmail' | 'deadlineMail' | 'recordEmail'
  | 'incidentOutbox' | 'recordIncident' | 'rollup' | 'backfillFinalize';

type Program = (
  helpers: Record<string, unknown>,
  input: { all: () => INodeExecutionData[]; first: () => INodeExecutionData | undefined },
  context: Record<string, unknown>,
) => Promise<unknown>;

// The scripts are generated from version-controlled, tested journal sources.
// Their code is fixed at package build time; workflow input never becomes code.
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor as
  new (...args: string[]) => Program;
const compiled = new Map<string, Program>();

function program(operation: JournalOperation): Program {
  let result = compiled.get(operation);
  if (!result) {
    const source = journalPrograms[operation];
    if (!source) throw new Error(`Unknown Dev Journal operation ${operation}`);
    result = new AsyncFunction('helpers', '$input', 'ctx', source);
    compiled.set(operation, result);
  }
  return result;
}

function object(value: unknown, field: string): Record<string, unknown> {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { throw new Error(`${field} must be JSON`); }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

function array(value: unknown, field: string): unknown[] {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { throw new Error(`${field} must be JSON`); }
  }
  if (!Array.isArray(value)) throw new Error(`${field} must be an array`);
  return value;
}

export class DevJournal implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Dev Journal',
    name: 'devJournal',
    icon: { light: 'file:bloodbank.svg', dark: 'file:bloodbank.dark.svg' },
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description: 'Durable Dev Journal report, issue, incident and rollup operations',
    defaults: { name: 'Dev Journal' },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [{ name: 'httpHeaderAuth', required: true,
      displayOptions: { show: { operation: ['processReport', 'backfillFinalize'] } } }],
    properties: [
      { displayName: 'Operation', name: 'operation', type: 'options', noDataExpression: true,
        default: 'ingest', options: [
          { name: 'Ingest Snapshot', value: 'ingest', action: 'Ingest a verified snapshot' },
          { name: 'Select Due Report', value: 'selectReport', action: 'Select one due report' },
          { name: 'Process Report', value: 'processReport', action: 'Extract and track report issues' },
          { name: 'Prepare Daily Mail', value: 'prepareEmail', action: 'Prepare a report email' },
          { name: 'Prepare 07:00 Mail', value: 'deadlineMail', action: 'Prepare pending or missing report mail' },
          { name: 'Record Email', value: 'recordEmail', action: 'Record a Resend receipt' },
          { name: 'Load Incident Outbox', value: 'incidentOutbox', action: 'Load unreported incidents' },
          { name: 'Record Incident', value: 'recordIncident', action: 'Record incident publication' },
          { name: 'Build Rollups', value: 'rollup', action: 'Build weekly and monthly rollups' },
          { name: 'Finalize Backfill Tickets', value: 'backfillFinalize', action: 'Create only unresolved historical tickets' },
        ] },
      { displayName: 'Selected Report (JSON)', name: 'selected', type: 'json', default: '{}',
        displayOptions: { show: { operation: ['processReport'] } },
        description: 'The report key and source candidates returned by Select Due Report' },
      { displayName: 'Prepared Mail (JSON)', name: 'mail', type: 'json', default: '{}',
        displayOptions: { show: { operation: ['recordEmail'] } },
        description: 'Prepared mail item whose Resend response is on the input' },
      { displayName: 'Published Incidents (JSON)', name: 'incidents', type: 'json', default: '[]',
        displayOptions: { show: { operation: ['recordIncident'] } },
        description: 'Incident outbox items published by the preceding Bloodbank node' },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const operation = this.getNodeParameter('operation', 0) as JournalOperation;
    const context: Record<string, unknown> = {};
    try {
      if (operation === 'processReport') {
        context.selected = object(this.getNodeParameter('selected', 0), 'selected');
      } else if (operation === 'recordEmail') {
        context.mail = object(this.getNodeParameter('mail', 0), 'mail');
      } else if (operation === 'recordIncident') {
        context.incidents = array(this.getNodeParameter('incidents', 0), 'incidents').map((json) => ({ json }));
      }
      const storage = journalStore();
      const helpers = {
        journalRows: (kind: string) => storage.rows(kind),
        journalOne: (kind: string, key: string, value: string) => storage.one(kind, key, value),
        journalUpsert: (kind: string, key: string, value: string, data: Record<string, unknown>) =>
          storage.upsert(kind, key, value, data),
        httpRequest: (options: unknown) => this.helpers.httpRequest(options as never),
        httpRequestWithAuthentication: (credential: string, options: unknown) =>
          this.helpers.httpRequestWithAuthentication.call(this, credential, options as never),
      };
      const input = this.getInputData();
      const output = await program(operation)(helpers, {
        all: () => input,
        first: () => input[0],
      }, context);
      if (!Array.isArray(output) || output.some((item) =>
        !item || typeof item !== 'object' || !('json' in item) ||
        !item.json || typeof item.json !== 'object' || Array.isArray(item.json))) {
        throw new Error(`Dev Journal ${operation} returned invalid n8n items`);
      }
      return [output as INodeExecutionData[]];
    } catch (error) {
      throw new NodeOperationError(this.getNode(),
        `Dev Journal ${operation}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}
