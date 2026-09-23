import type {
  IDataObject,
  INodeType,
  INodeTypeDescription,
  IRun,
  ITriggerFunctions,
  ITriggerResponse,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { bindingMatches, bindingSubject, canonicalTypeFor, sampleEnvelope } from '../../bindings';
import { consumeDurable, durableConsumerName, durableTransport } from '../../durable';
import type { DurableVerdict } from '../../durable';
import {
  COMMANDS_STREAM,
  EVENTS_STREAM,
  findLatestMatching,
  withDirectGet,
} from '../../jetstream';
import type { StoredMessage } from '../../jetstream';
import { matchesDataConditions, parseDataConditions } from '../../match';
import type { DataCondition } from '../../match';
import { publishReply, subjectFor, subscribe } from '../../nats';
import { commandOptions, eventOptions } from '../../options';
import { commandSchemas } from '../Bloodbank/eventSchemas';

type Kind = 'event' | 'command';
type TestEventSource = 'replay' | 'sample' | 'live';

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

/** Does an envelope pass this trigger's bindings and data filter? */
export function triggerAccepts(
  kind: Kind,
  bindings: string[],
  conditions: DataCondition[],
  envelope: Record<string, unknown>,
): boolean {
  const bound = kind === 'event'
    ? bindings.some((binding) => bindingMatches(binding, envelope))
    : envelope.type === bindings[0];
  return bound && matchesDataConditions(envelope, conditions);
}

/** What one delivered message means to this trigger. */
export type TriggerDecision =
  | { verdict: 'emit'; envelope: Record<string, unknown> }
  | { verdict: 'filtered' }
  | { verdict: 'rejected'; reason: string };

/** Decode, bind and filter one delivered message, before anything is emitted.
 *
 * Shared by the core-NATS and the durable paths so both accept and refuse
 * exactly the same messages. `filtered` is a well-formed message this trigger
 * is not bound to (another alias, another data value); `rejected` is a
 * malformed one, which becomes a failed execution.
 */
export function decideTriggerMessage(
  kind: Kind,
  bindings: string[],
  conditions: DataCondition[],
  data: Uint8Array,
): TriggerDecision {
  try {
    const decoded = JSON.parse(Buffer.from(data).toString('utf8'));
    const envelope = record(decoded);
    if (!envelope) throw new Error('envelope must be a JSON object');
    if (envelope.kind !== kind) {
      throw new Error(`subject delivered kind=${String(envelope.kind)} to ${kind} trigger`);
    }
    // Bindings and the data filter both decide BEFORE emit: a message
    // that fails either never becomes an execution.
    if (!triggerAccepts(kind, bindings, conditions, envelope)) return { verdict: 'filtered' };
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
    return { verdict: 'emit', envelope };
  } catch (error) {
    return { verdict: 'rejected', reason: (error as Error).message };
  }
}

/** Wait for an emitted execution to finish, bounded; never throws. */
export async function awaitExecution(done: Promise<unknown>, maxWaitMs: number): Promise<'finished' | 'failed' | 'timeout'> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      done.then(
        () => 'finished' as const,
        () => 'failed' as const,
      ),
      new Promise<'timeout'>((resolve) => {
        timer = setTimeout(() => resolve('timeout'), maxWaitMs);
        timer.unref?.();
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Longest an execution may hold its trigger's queue before the next message goes. */
export const EXECUTION_WAIT_CAP_MS = 5 * 60 * 1000;

export interface ReplayResult {
  envelope: Record<string, unknown>;
  origin: 'replay' | 'sample';
  stored?: Pick<StoredMessage, 'seq' | 'subject' | 'time'>;
  note?: string;
}

/** What a manual test run emits: the newest real match, else a sample. */
export async function manualTestEnvelope(
  kind: Kind,
  bindings: string[],
  conditions: DataCondition[],
  source: Exclude<TestEventSource, 'live'>,
  replay: (stream: string, subject: string, accepts: (envelope: Record<string, unknown>) => boolean) => Promise<StoredMessage | null>,
): Promise<ReplayResult> {
  let note: string | undefined;
  if (source === 'replay') {
    const stream = kind === 'event' ? EVENTS_STREAM : COMMANDS_STREAM;
    const subjects = [...new Set(bindings.map((binding) => bindingSubject(binding, kind)))];
    let best: StoredMessage | null = null;
    try {
      for (const subject of subjects) {
        const found = await replay(stream, subject, (envelope) =>
          triggerAccepts(kind, bindings, conditions, envelope),
        );
        if (found && (!best || found.seq > best.seq)) best = found;
      }
    } catch (error) {
      note = `replay unavailable: ${(error as Error).message}`;
    }
    if (best) {
      return {
        envelope: best.envelope,
        origin: 'replay',
        stored: { seq: best.seq, subject: best.subject, time: best.time },
      };
    }
    note = note || 'no retained message matched; emitted a generated sample';
  }
  return { envelope: sampleEnvelope(bindings[0], kind), origin: 'sample', note };
}

export class BloodbankTrigger implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Bloodbank Trigger',
    name: 'bloodbankTrigger',
    icon: { light: 'file:bloodbank.svg', dark: 'file:bloodbank.dark.svg' },
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
        options: eventOptions({ includeAliases: true }),
        default: [],
        required: true,
        displayOptions: { show: { messageKind: ['event'] } },
        description:
          'Bind any number of canonical Bloodbank events or provider aliases. An alias (e.g. Plane · On Ticket Created) subscribes to its canonical subject and filters data.provider_event_type.',
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
        displayName: 'Only When Data Matches',
        name: 'dataMatch',
        type: 'fixedCollection',
        typeOptions: { multipleValues: true },
        placeholder: 'Add condition',
        default: {},
        description:
          'Drop a message before it starts an execution unless every condition holds. A message that fails never becomes an execution, so a busy subject costs nothing for the ones you do not want.',
        options: [
          {
            name: 'conditions',
            displayName: 'Condition',
            values: [
              {
                displayName: 'Path',
                name: 'path',
                type: 'string',
                default: '',
                placeholder: 'data.context.reason',
                description: 'Dot path into the whole envelope, e.g. data.context.reason, data.provider, type',
              },
              {
                displayName: 'Values',
                name: 'values',
                type: 'string',
                default: '',
                placeholder: 'ticket-grooming,ticket-delegation',
                description:
                  'Comma-separated. The value at Path must equal one of them (any element, for an array). Leave empty to require only that Path is present and non-empty.',
              },
            ],
          },
        ],
      },
      {
        displayName: 'Delivery',
        name: 'delivery',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Durable',
            value: 'durable',
            description:
              'A JetStream durable consumer per workflow and node. Events published while n8n restarts or the workflow is re-saved are delivered when it is back, in order.',
          },
          {
            name: 'Ephemeral',
            value: 'ephemeral',
            description:
              'A plain NATS subscription. Only hears what is published while the workflow is active; anything sent during a restart or re-save is lost.',
          },
        ],
        default: 'durable',
        displayOptions: { show: { messageKind: ['event'] } },
        description:
          'How an active workflow receives events. The durable is named n8n-<workflow id>-<node id> on BLOODBANK_EVENTS, starts at the newest event on first activation, survives deactivation, and is deleted by the server after 7 days unused.',
      },
      {
        displayName: 'Acknowledge',
        name: 'acknowledge',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'After Execution Finishes',
            value: 'afterExecution',
            description:
              'One execution at a time, in stream order; the event is acknowledged when its execution ends (success or error). A crash mid-execution redelivers it.',
          },
          {
            name: 'On Emit',
            value: 'onEmit',
            description:
              'Acknowledge as soon as the execution starts; executions may overlap and finish out of order. For long-running workflows.',
          },
        ],
        default: 'afterExecution',
        displayOptions: { show: { messageKind: ['event'], delivery: ['durable'] } },
      },
      {
        displayName: 'Catch-Up Window (Hours)',
        name: 'catchUpHours',
        type: 'number',
        typeOptions: { minValue: 0 },
        default: 24,
        displayOptions: { show: { messageKind: ['event'], delivery: ['durable'] } },
        description:
          'Events older than this when they are delivered are acknowledged and skipped instead of starting an execution, so re-activating a workflow after a long break does not replay days of history. 0 = no limit (the stream keeps 7 days).',
      },
      {
        displayName: 'Test Event Source',
        name: 'testEventSource',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Replay Last Matching',
            value: 'replay',
            description:
              'Emit the newest retained message that passes the bindings and data filter (JetStream direct get; no consumer is created), else a generated sample',
          },
          {
            name: 'Generated Sample',
            value: 'sample',
            description: 'Emit a sample envelope built from the bound schema',
          },
          {
            name: 'Wait for Live',
            value: 'live',
            description: 'Subscribe and wait for the next matching message',
          },
        ],
        default: 'replay',
        description:
          'What a manual "Test step" or "Test workflow" emits. An active workflow always consumes live messages.',
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
    const kind = this.getNodeParameter('messageKind') as Kind;
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
    for (const binding of bindings) {
      try {
        canonicalTypeFor(binding);
      } catch (error) {
        throw new NodeOperationError(this.getNode(), (error as Error).message);
      }
    }
    const conditions = parseDataConditions(this.getNodeParameter('dataMatch', {}));
    const connection = this.getNodeParameter('connection', {}) as {
      natsHost?: string;
      natsPort?: number;
      timeoutMs?: number;
    };
    const natsOptions = {
      host: connection.natsHost || undefined,
      port: connection.natsPort ? Number(connection.natsPort) : undefined,
      timeoutMs: connection.timeoutMs ? Number(connection.timeoutMs) : undefined,
    };

    const testSource = String(this.getNodeParameter('testEventSource', 'replay')) as TestEventSource;
    if (this.getMode() === 'manual' && testSource !== 'live') {
      const manualTriggerFunction = async (): Promise<void> => {
        const result = await manualTestEnvelope(
          kind,
          bindings,
          conditions,
          testSource,
          (stream, subject, accepts) =>
            withDirectGet(natsOptions, (get) => findLatestMatching(get, stream, subject, accepts)),
        );
        // The item is exactly what a live delivery would carry. A generated
        // sample is recognisable by its top-level `sample: true` extension.
        this.emit([[{ json: result.envelope as IDataObject }]]);
      };
      return { closeFunction: async () => {}, manualTriggerFunction };
    }

    const subjects = kind === 'event'
      ? [...new Set(bindings.map((binding) => bindingSubject(binding, 'event')))]
      : [subjectFor(bindings[0], 'command')];
    const queue = kind === 'command'
      ? String(this.getNodeParameter('queueGroup')).trim()
      : undefined;
    if (kind === 'command' && !queue) {
      throw new NodeOperationError(this.getNode(), 'Queue Group must not be empty');
    }

    const reject = (subject: string, reason: string): void => {
      this.saveFailedExecution(
        new NodeOperationError(
          this.getNode(),
          `Rejected malformed Bloodbank message on ${subject}: ${reason}`,
        ),
      );
    };

    // Durable delivery: an active event trigger that has not opted out. Manual
    // "Wait for Live" tests always use a throwaway subscription, so a test
    // never competes with (or acknowledges for) the active workflow's durable.
    const delivery = kind === 'event' && this.getMode() !== 'manual'
      ? String(this.getNodeParameter('delivery', 'durable'))
      : 'ephemeral';
    if (delivery === 'durable') {
      const workflow = this.getWorkflow();
      const node = this.getNode();
      let durable: string;
      try {
        durable = durableConsumerName(workflow.id, node.id);
      } catch (error) {
        throw new NodeOperationError(node, (error as Error).message);
      }
      const acknowledge = String(this.getNodeParameter('acknowledge', 'afterExecution'));
      const catchUpHours = Number(this.getNodeParameter('catchUpHours', 24));
      const logger = this.logger;
      const backend = await durableTransport.backend({
        ...natsOptions,
        stream: EVENTS_STREAM,
        name: durable,
        subjects,
        description: `n8n "${workflow.name ?? workflow.id}" (${workflow.id}) node "${node.name}"`,
        onEnsured: (result) => {
          if (result.action !== 'unchanged') {
            logger.info(`Bloodbank durable ${durable} ${result.action}`, {
              durable,
              drift: result.drift,
              workflowId: workflow.id,
            });
          }
        },
      });
      let stale = 0;
      const subscription = await consumeDurable(backend, {
        catchUpWindowMs: Number.isFinite(catchUpHours) && catchUpHours > 0 ? catchUpHours * 3600_000 : 0,
        onStale: (message, ageMs) => {
          stale += 1;
          logger.warn(
            `Bloodbank durable ${durable} skipped ${message.subject} #${message.seq}: ${Math.round(ageMs / 60000)} min old, outside the catch-up window (${stale} so far)`,
          );
        },
        onWarning: (message, error) =>
          logger.warn(`Bloodbank durable ${durable}: ${message}${error ? `: ${error.message}` : ''}`),
        onFatal: (error) => this.emitError(error),
        onMessage: async (message): Promise<DurableVerdict> => {
          const decision = decideTriggerMessage(kind, bindings, conditions, message.data);
          if (decision.verdict === 'filtered') return 'ack';
          if (decision.verdict === 'rejected') {
            reject(message.subject, decision.reason);
            return 'term';
          }
          const item = { json: decision.envelope as IDataObject };
          if (acknowledge === 'onEmit') {
            this.emit([[item]]);
            return 'ack';
          }
          const donePromise = this.helpers.createDeferredPromise<IRun>();
          this.emit([[item]], undefined, donePromise);
          const outcome = await awaitExecution(donePromise.promise, EXECUTION_WAIT_CAP_MS);
          if (outcome === 'timeout') {
            logger.warn(
              `Bloodbank durable ${durable}: execution for ${message.subject} #${message.seq} still running after ${EXECUTION_WAIT_CAP_MS / 1000}s; acknowledged and moving on`,
            );
          }
          // A failed execution is still acknowledged: n8n recorded it, and a
          // redelivery would fail the same way.
          return 'ack';
        },
      });
      return {
        closeFunction: () => subscription.close(),
      };
    }

    const subscription = await subscribe({
      subjects,
      queue,
      ...natsOptions,
      name: `n8n-bloodbank-${kind}-trigger`,
      onError: (error) => this.emitError(error),
      onMessage: async (message) => {
        const decision = decideTriggerMessage(kind, bindings, conditions, message.data);
        if (decision.verdict === 'filtered') return;
        if (decision.verdict === 'rejected') {
          reject(message.subject, decision.reason);
          return;
        }
        const envelope = decision.envelope;

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
