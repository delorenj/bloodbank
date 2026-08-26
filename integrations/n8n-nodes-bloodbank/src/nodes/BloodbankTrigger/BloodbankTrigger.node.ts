import type {
  IDataObject,
  INodeProperties,
  INodeType,
  INodeTypeDescription,
  IRun,
  ITriggerFunctions,
  ITriggerResponse,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { publishReply, subjectFor, subscribe } from '../../nats';
import { planeBindingMatches, planeEventBindings } from '../../plane';
import { commandSchemas, eventSchemas } from '../Bloodbank/eventSchemas';

function eventOptions(): NonNullable<INodeProperties['options']> {
  const canonical = eventSchemas.map((schema) => ({
    name: schema.type,
    value: schema.type,
    description: schema.description || schema.title,
  }));
  const plane = planeEventBindings.map(({ name, value, description }) => ({
    name,
    value,
    description,
  }));
  return [...plane, ...canonical];
}

function commandOptions(): NonNullable<INodeProperties['options']> {
  return commandSchemas.map((schema) => ({
    name: schema.type,
    value: schema.type,
    description: schema.description || schema.title,
  }));
}

function eventSubject(binding: string): string {
  if (binding.startsWith('bloodbank.')) return subjectFor(binding, 'event');
  const alias = planeEventBindings.find((candidate) => candidate.value === binding);
  if (!alias) throw new Error(`unknown Bloodbank event binding: ${binding}`);
  return subjectFor(alias.canonicalType, 'event');
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function executionResult(run: IRun): Record<string, unknown> {
  const runData = run.data.resultData.runData;
  const entries = Object.entries(runData);
  const terminal = entries.length ? entries[entries.length - 1] : undefined;
  const attempts = terminal?.[1] || [];
  const task = attempts.length ? attempts[attempts.length - 1] : undefined;
  const items = task?.data?.main?.flatMap((output) => output || []).map((item) => item.json) || [];
  return {
    execution_status: run.status,
    finished: run.finished ?? run.status === 'success',
    terminal_node: terminal?.[0] ?? null,
    output: items,
  };
}

async function commandRun(
  donePromise: { promise: Promise<IRun> },
  envelope: Record<string, unknown>,
): Promise<IRun> {
  const requested = Number(envelope.timeout_ms);
  const timeoutMs = Number.isFinite(requested) && requested > 0
    ? Math.min(requested, 15 * 60 * 1000)
    : 30 * 1000;
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      donePromise.promise,
      new Promise<IRun>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error(`synchronous command timed out after ${timeoutMs}ms`)),
          timeoutMs,
        );
        timer.unref();
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export class BloodbankTrigger implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Bloodbank Trigger',
    name: 'bloodbankTrigger',
    group: ['trigger'],
    version: 1,
    subtitle: '={{$parameter["messageKind"]}}',
    description: 'Start a workflow from Bloodbank events or single-consumer commands',
    defaults: { name: 'Bloodbank Trigger' },
    inputs: [],
    outputs: ['main'],
    properties: [
      {
        displayName: 'Message Kind',
        name: 'messageKind',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Events',
            value: 'event',
            description: 'Fan-out facts. Select one or more; delivery is always asynchronous.',
          },
          {
            name: 'Command',
            value: 'command',
            description: 'A single command handled through a NATS queue group.',
          },
        ],
        default: 'event',
      },
      {
        displayName: 'Events',
        name: 'events',
        type: 'multiOptions',
        noDataExpression: true,
        options: eventOptions(),
        default: [],
        required: true,
        displayOptions: { show: { messageKind: ['event'] } },
        description:
          'Bind any number of canonical Bloodbank events or Plane provenance aliases. Plane aliases filter provider_event_type while subscribing to the canonical repo.* subject.',
      },
      {
        displayName: 'Command',
        name: 'command',
        type: 'options',
        noDataExpression: true,
        options: commandOptions(),
        default: commandSchemas.length ? commandSchemas[0].type : '',
        required: true,
        displayOptions: { show: { messageKind: ['command'] } },
        description: 'Bind exactly one schema-registered Bloodbank command.',
      },
      {
        displayName: 'Command Processing',
        name: 'commandProcessing',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Asynchronous',
            value: 'async',
            description: 'Start the workflow and do not wait or publish a reply.',
          },
          {
            name: 'Synchronous',
            value: 'sync',
            description: 'Wait for workflow completion and publish a correlated Bloodbank reply.',
          },
        ],
        default: 'async',
        displayOptions: { show: { messageKind: ['command'] } },
      },
      {
        displayName: 'Queue Group',
        name: 'queueGroup',
        type: 'string',
        default: 'n8n-bloodbank-commands',
        required: true,
        displayOptions: { show: { messageKind: ['command'] } },
        description: 'Consumers in the same group compete so one workflow receives each command.',
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
            displayName: 'Connect Timeout (ms)',
            name: 'timeoutMs',
            type: 'number',
            default: 5000,
          },
        ],
      },
    ],
  };

  async trigger(this: ITriggerFunctions): Promise<ITriggerResponse> {
    const kind = this.getNodeParameter('messageKind') as 'event' | 'command';
    const processing = kind === 'command'
      ? (this.getNodeParameter('commandProcessing') as 'async' | 'sync')
      : 'async';
    const bindings = kind === 'event'
      ? (this.getNodeParameter('events') as string[])
      : [this.getNodeParameter('command') as string];
    if (!bindings.length || bindings.some((binding) => !binding)) {
      throw new NodeOperationError(this.getNode(), `Select at least one Bloodbank ${kind}`);
    }
    if (kind === 'command' && bindings.length !== 1) {
      throw new NodeOperationError(this.getNode(), 'A command trigger must bind exactly one command');
    }
    const connection = this.getNodeParameter('connection', {}) as {
      natsHost?: string;
      natsPort?: number;
      timeoutMs?: number;
    };
    const subjects = kind === 'event'
      ? bindings.map(eventSubject)
      : [subjectFor(bindings[0], 'command')];
    const queue = kind === 'command'
      ? String(this.getNodeParameter('queueGroup')).trim()
      : undefined;
    if (kind === 'command' && !queue) {
      throw new NodeOperationError(this.getNode(), 'Queue Group must not be empty');
    }

    const subscription = await subscribe({
      subjects,
      queue,
      host: connection.natsHost || undefined,
      port: connection.natsPort ? Number(connection.natsPort) : undefined,
      timeoutMs: connection.timeoutMs ? Number(connection.timeoutMs) : undefined,
      name: `n8n-bloodbank-${kind}-trigger`,
      onError: (error) => this.emitError(error),
      onMessage: async (message) => {
        let envelope: Record<string, unknown>;
        try {
          const decoded = JSON.parse(Buffer.from(message.data).toString('utf8'));
          const candidate = record(decoded);
          if (!candidate) throw new Error('envelope must be a JSON object');
          envelope = candidate;
          if (envelope.kind !== kind) {
            throw new Error(`subject delivered kind=${String(envelope.kind)} to ${kind} trigger`);
          }
          const matches = kind === 'event'
            ? bindings.some(
                (binding) => binding === envelope.type || planeBindingMatches(binding, envelope),
              )
            : envelope.type === bindings[0];
          if (!matches) return;
          if (kind === 'command' && envelope.delivery !== 'single_consumer') {
            throw new Error('command delivery must be single_consumer');
          }
          if (
            kind === 'command' &&
            (typeof envelope.command_id !== 'string' || !envelope.command_id.trim())
          ) {
            throw new Error('command_id must be a non-empty string');
          }
          if (
            kind === 'command' &&
            (typeof envelope.idempotency_key !== 'string' || !envelope.idempotency_key.trim())
          ) {
            throw new Error('idempotency_key must be a non-empty string');
          }
        } catch (error) {
          this.saveFailedExecution(
            new NodeOperationError(
              this.getNode(),
              `Rejected malformed Bloodbank message on ${message.subject}: ${(error as Error).message}`,
            ),
          );
          return;
        }

        const item = { json: envelope as IDataObject };
        if (kind === 'event' || processing === 'async') {
          this.emit([[item]]);
          return;
        }

        const donePromise = this.helpers.createDeferredPromise<IRun>();
        this.emit([[item]], undefined, donePromise);
        try {
          const run = await commandRun(donePromise, envelope);
          const result = executionResult(run);
          const status = run.status === 'success' ? 'SUCCESS' : 'ERROR';
          await publishReply(message.publish, envelope, status, result, message.replySubject);
        } catch (error) {
          await publishReply(message.publish, envelope, 'ERROR', {
            execution_status: 'error',
            error: (error as Error).message.slice(0, 500),
          }, message.replySubject);
        }
      },
    });

    return {
      closeFunction: () => subscription.close(),
    };
  }
}
