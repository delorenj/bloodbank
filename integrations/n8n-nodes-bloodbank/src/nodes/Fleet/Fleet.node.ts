import { readFile } from 'node:fs/promises';

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import {
  delegationPrompt,
  fleetCommandId,
  groomingPrompt,
  resolveFleetAgentForBoard,
  ticketCorrelationId,
  ticketFactsFromEnvelope,
} from '../../fleet';
import type { FleetRoute, TicketFacts } from '../../fleet';
import { splitList } from '../../match';
import { deterministicUuid, publish } from '../../nats';
import { providerAliasOptions } from '../../options';
import { hermesRegistryPath, loadHermesRegistry } from '../../registry';

const INVOCATION_COMMAND_TYPE = 'bloodbank.agent.invocation.start';
const INVOCATION_SKIPPED_TYPE = 'bloodbank.agent.invocation.skipped';
const GROOMED_LABEL = 'lifecycle:triaged';
const FLEET_SOURCE = 'urn:33god:integration:n8n:agent-fleet';

export type SkipCode =
  | 'provider_event_guard'
  | 'phase_guard'
  | 'ineligible'
  | 'invalid_policy'
  | 'fenced'
  | 'no_route';

function nonblank(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value.trim();
}

/** A mapped parameter's value, or undefined when it resolved to nothing.
 *
 * Mapping parameters default to expressions over the incoming envelope. When
 * the path is missing, n8n may hand back undefined, null, '' or — for a
 * stringified miss — the literal text; all of them mean "not mapped".
 */
function optionalText(value: unknown): string | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (!trimmed || trimmed === 'undefined' || trimmed === 'null') return undefined;
  return trimmed;
}

function jsonObject(value: unknown, name: string): Record<string, unknown> {
  let parsed = value;
  if (parsed === undefined || parsed === null) return {};
  if (typeof value === 'string') {
    try {
      parsed = value.trim() ? JSON.parse(value) : {};
    } catch {
      throw new Error(`${name} must be valid JSON`);
    }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${name} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

/** Case-insensitive membership, so a guard can name a state or its group. */
function matchesAny(candidate: string, allowed: string[]): boolean {
  if (!allowed.length) return true;
  const needle = candidate.trim().toLowerCase();
  return allowed.some((entry) => entry.toLowerCase() === needle);
}

function invocationReason(operation: string): string {
  if (operation === 'groomTicket') return 'ticket-grooming';
  if (operation === 'delegateTicket') return 'ticket-delegation';
  return 'fleet-invoke';
}

/** `.project.json` `execution.mode`, or 'legacy' when there is nothing to read.
 *
 * A repo with no manifest, an unreadable one, or one that is not JSON has not
 * opted into Krebs-managed execution, and that is exactly what legacy means.
 * The fence must never be the reason a dispatch crashes.
 */
export async function executionMode(projectPath: string): Promise<string> {
  try {
    const manifest = JSON.parse(await readFile(`${projectPath}/.project.json`, 'utf8'));
    const execution = manifest && typeof manifest === 'object' ? manifest.execution : undefined;
    const mode = execution && typeof execution === 'object' ? execution.mode : undefined;
    return typeof mode === 'string' && mode.trim() ? mode.trim() : 'legacy';
  } catch {
    return 'legacy';
  }
}

const RFC3339 =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

/** The causing event's time, as the command's `time`, so a replay is byte-identical.
 *
 * The gateway journals a command under its command_id AND a sha256 of the
 * whole envelope. With a wall-clock `time`, a redelivered trigger event made
 * the same command_id with a different digest, which the gateway terminally
 * rejects as a collision. Stamping the causing event's own time makes the
 * rebuilt envelope identical, so the gateway recognises it as the command it
 * already has (in flight: nak and retry later; finished: replay the journaled
 * outcome) instead. A valid RFC 3339 value is kept verbatim; anything else that
 * still parses is normalised; unusable input falls back to now.
 */
export function stableObservedAt(value: unknown): string | undefined {
  const raw = optionalText(value);
  if (!raw) return undefined;
  if (RFC3339.test(raw) && !Number.isNaN(Date.parse(raw))) return raw;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? undefined : new Date(parsed).toISOString();
}

const mapping = (
  displayName: string,
  name: string,
  expression: string,
  description: string,
  extra: Partial<INodeProperties> = {},
): INodeProperties => ({
  displayName,
  name,
  type: 'string',
  default: expression,
  description,
  ...extra,
});

export class Fleet implements INodeType {
  description: INodeTypeDescription = {
    displayName: '33GOD Agent Fleet',
    name: 'bloodbankFleet',
    icon: { light: 'file:fleet.svg', dark: 'file:fleet.dark.svg' },
    group: ['output'],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description:
      'Hand a ticket to the fleet agent that owns its board — registry-resolved, one thread per ticket, every non-dispatch on its own output and on the bus',
    defaults: { name: '33GOD Agent Fleet' },
    inputs: ['main'],
    outputs: ['main', 'main'],
    outputNames: ['Dispatched', 'Skipped'],
    usableAsTool: true,
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Groom Ticket',
            value: 'groomTicket',
            action: 'Groom a newly created ticket',
            description:
              'Ask the board\'s PM to enrich one new ticket in place — labels, module, priority, cycle, acceptance criteria — without splitting it or changing its state',
          },
          {
            name: 'Delegate Ticket',
            value: 'delegateTicket',
            action: 'Delegate a groomed ticket to the project PM',
            description:
              'Ask the board\'s PM to pick up a groomed ticket that reached Todo, delegate the work, and move it to In Progress',
          },
          {
            name: 'Invoke Agent',
            value: 'invoke',
            action: 'Invoke the board owning agent with your own prompt',
            description: 'Send an arbitrary prompt to the fleet agent that owns a board',
          },
        ],
        default: 'groomTicket',
        required: true,
      },
      {
        displayName: 'Prompt',
        name: 'prompt',
        type: 'string',
        typeOptions: { rows: 6 },
        default: '',
        required: true,
        displayOptions: { show: { operation: ['invoke'] } },
        description: 'Instruction for the resolved fleet agent',
      },
      {
        displayName: 'Completion Label',
        name: 'groomedLabel',
        type: 'string',
        default: GROOMED_LABEL,
        displayOptions: { show: { operation: ['groomTicket'] } },
        description:
          'Label the grooming pass adds when it finishes, and the one Delegate Ticket requires. Clear it to stamp nothing.',
      },
      {
        displayName: 'Required Label',
        name: 'requiredLabel',
        type: 'string',
        default: GROOMED_LABEL,
        displayOptions: { show: { operation: ['delegateTicket'] } },
        description:
          'Label that marks a ticket as groomed. The agent grooms the ticket first when it is missing. Clear it to delegate ungroomed tickets.',
      },
      {
        displayName: 'Only When Phase Is',
        name: 'phaseGuard',
        type: 'string',
        default: 'Todo,unstarted',
        displayOptions: { show: { operation: ['delegateTicket'] } },
        description:
          'Comma-separated state names or groups. The item is skipped unless the ticket landed in one of them. Clear to accept any phase.',
      },
      {
        displayName: 'Only When Provider Event Is',
        name: 'providerEventGuard',
        type: 'multiOptions',
        options: providerAliasOptions(),
        default: [],
        description:
          'Skip the item unless data.provider_event_type is one of these. Strict: with any selected, an item that carries no provider_event_type is skipped too. Select none to accept every item.',
      },
      {
        displayName:
          'Ticket fields below default to the incoming Bloodbank envelope. Clear one to fall back to lifting it from the envelope; set one to override it.',
        name: 'mappingNotice',
        type: 'notice',
        default: '',
      },
      mapping('Repository', 'repo', '={{ $json.data?.repo }}', 'Repo slug of the ticket\'s project'),
      mapping(
        'Board ID',
        'boardId',
        '={{ $json.data?.board_id ?? $json.data?.project_id }}',
        'Provider board id. Resolves the owning agent before the repo slug does.',
      ),
      mapping('Ticket Key', 'ticketKey', '={{ $json.data?.ticket_key }}', 'Human ticket key, e.g. JIMB-273'),
      mapping(
        'Ticket ID',
        'ticketId',
        '={{ $json.data?.ticket_id ?? $json.data?.task_id ?? $json.data?.ticket?.id }}',
        'Provider ticket id. Keys the ticket\'s conversation (correlation) when the envelope carries none.',
      ),
      mapping('Title', 'title', '={{ $json.data?.title ?? $json.data?.ticket?.name }}', 'Ticket title'),
      mapping('Workspace', 'workspace', '={{ $json.data?.workspace }}', 'Provider workspace slug'),
      mapping(
        'Provider Event Type',
        'providerEventType',
        '={{ $json.data?.provider_event_type }}',
        'Provider provenance the guard above checks, e.g. plane.ticket.created',
      ),
      mapping('Phase', 'phase', '={{ $json.data?.phase }}', 'State the ticket landed in', {
        displayOptions: { show: { operation: ['delegateTicket'] } },
      }),
      mapping(
        'Correlation ID',
        'correlationId',
        '={{ $json.correlationid }}',
        'Thread the command joins. Blank: derived from the ticket (plane:<board>:<ticket id>).',
      ),
      mapping(
        'Causation ID',
        'causationId',
        '={{ $json.id }}',
        'Event that caused this dispatch. Also makes the command id — and so its idempotency key — deterministic.',
      ),
      mapping(
        'Observed At',
        'observedAt',
        '={{ $json.time }}',
        'Time of the event that caused this dispatch, stamped as the command time so a redelivered trigger rebuilds a byte-identical command the gateway de-duplicates. Blank: now.',
      ),
      {
        displayName: 'Publish Skip Events',
        name: 'publishSkips',
        type: 'boolean',
        default: true,
        description:
          'Whether to publish bloodbank.agent.invocation.skipped for every item sent to the Skipped output, so a ticket nobody picked up is explainable from the bus',
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
      {
        displayName: 'Service Name',
        name: 'service',
        type: 'string',
        default: 'n8n-agent-fleet',
        description: 'Recorded as the command producer, so executions are traceable to a workflow',
      },
      // Retired UI parameters. Kept as hidden values so workflows saved before
      // 0.5.0 keep their meaning; n8n drops parameters a node no longer
      // declares, so deleting these would silently change saved behaviour.
      { displayName: 'Ticket (legacy overrides)', name: 'ticket', type: 'hidden', default: {} },
      { displayName: 'Hermes Registry File (legacy)', name: 'registryFile', type: 'hidden', default: '' },
      { displayName: 'On Ineligible Agent (legacy)', name: 'onIneligible', type: 'hidden', default: 'skip' },
    ],
  };

  async execute(
    this: IExecuteFunctions,
    publishMessage?: unknown,
  ): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const dispatched: INodeExecutionData[] = [];
    const skipped: INodeExecutionData[] = [];
    const send = typeof publishMessage === 'function' ? (publishMessage as typeof publish) : publish;
    let registry: { path: string; value: unknown } | undefined;

    for (let i = 0; i < items.length; i++) {
      try {
        const operation = this.getNodeParameter('operation', i, 'groomTicket') as string;
        const conn = this.getNodeParameter('connection', i, {}) as {
          natsHost?: string;
          natsPort?: number;
          timeoutMs?: number;
        };
        const service = optionalText(this.getNodeParameter('service', i, '')) || 'n8n-agent-fleet';
        const param = (name: string): string | undefined =>
          optionalText(this.getNodeParameter(name, i, ''));

        // Legacy `ticket` collection: explicit overrides saved before 0.5.0.
        const legacy = jsonObject(this.getNodeParameter('ticket', i, {}), 'ticket overrides');
        const declared = optionalText(legacy.event);
        const envelope = declared
          ? jsonObject(declared, 'ticket event')
          : ((items[i].json || {}) as Record<string, unknown>);
        const lifted = ticketFactsFromEnvelope(envelope);
        // With a legacy declared event, the mapping expressions still point at
        // the input item, not at that event, so only the legacy values apply.
        const mapped = (name: string): string | undefined => (declared ? undefined : param(name));
        const pick = (legacyKey: string, name: string, fallback: string): string =>
          optionalText(legacy[legacyKey]) || mapped(name) || fallback;
        const facts: TicketFacts = {
          ...lifted,
          repo: pick('repo', 'repo', lifted.repo),
          boardId: pick('boardId', 'boardId', lifted.boardId),
          ticketKey: pick('ticketKey', 'ticketKey', lifted.ticketKey),
          ticketId: pick('ticketId', 'ticketId', lifted.ticketId),
          title: pick('title', 'title', lifted.title),
          workspace: pick('workspace', 'workspace', lifted.workspace),
          providerEventType: pick('providerEventType', 'providerEventType', lifted.providerEventType),
          phase: operation === 'delegateTicket' ? pick('phase', 'phase', lifted.phase) : lifted.phase,
        };
        const correlationId =
          mapped('correlationId') ||
          optionalText(envelope.correlationid) ||
          ticketCorrelationId(facts.boardId, facts.ticketId || facts.ticketKey);
        const causationId = mapped('causationId') || optionalText(envelope.id);
        const observedAt = stableObservedAt(mapped('observedAt') || optionalText(envelope.time));
        const reasonForInvocation = invocationReason(operation);

        const ticketJson = {
          operation,
          repo: facts.repo,
          boardId: facts.boardId,
          ticketKey: facts.ticketKey,
          ticketId: facts.ticketId,
          title: facts.title,
          workspace: facts.workspace,
          phase: facts.phase,
          providerEventType: facts.providerEventType,
        };

        const skip = async (code: SkipCode, reason: string, route?: FleetRoute): Promise<void> => {
          const target = route && code !== 'no_route' ? route.agentId || null : null;
          const json: Record<string, unknown> = {
            invoked: false,
            skipped: true,
            code,
            reason,
            ...ticketJson,
            agentId: target,
            matchedBy: route?.matchedBy ?? 'none',
          };
          if (this.getNodeParameter('publishSkips', i, true) !== false) {
            try {
              const result = await send({
                type: INVOCATION_SKIPPED_TYPE,
                kind: 'event',
                validate: true,
                data: {
                  reason,
                  skip_code: code,
                  operation,
                  target_agent_id: target,
                  matched_by: route?.matchedBy ?? 'none',
                  context: {
                    reason: reasonForInvocation,
                    repo: facts.repo || null,
                    ticket_key: facts.ticketKey || null,
                    ticket_id: facts.ticketId || null,
                    board_id: facts.boardId || null,
                    workspace: facts.workspace || null,
                    title: facts.title || null,
                    phase: facts.phase || null,
                    provider_event_type: facts.providerEventType || null,
                  },
                },
                eventId: causationId
                  ? deterministicUuid(`agent.invocation.skipped:${operation}:${causationId}`)
                  : undefined,
                correlationId,
                causationId,
                observedAt,
                orderingKey: facts.ticketId
                  ? `task:${facts.repo || facts.boardId || 'unknown'}:${facts.ticketId}`
                  : undefined,
                source: FLEET_SOURCE,
                producer: 'n8n',
                service,
                host: conn.natsHost || undefined,
                port: conn.natsPort ? Number(conn.natsPort) : undefined,
                timeoutMs: conn.timeoutMs ? Number(conn.timeoutMs) : undefined,
              });
              json.skipEvent = { published: true, subject: result.subject, eventId: result.eventId };
            } catch (error) {
              // Losing the audit event must not lose the item.
              json.skipEvent = { published: false, error: (error as Error).message };
            }
          } else {
            json.skipEvent = { published: false, disabled: true };
          }
          skipped.push({ json: json as never, pairedItem: { item: i } });
        };

        const providerGuard = splitList(this.getNodeParameter('providerEventGuard', i, []));
        if (providerGuard.length) {
          if (!facts.providerEventType) {
            await skip(
              'provider_event_guard',
              `provider_event_type is absent, not ${providerGuard.join(' or ')}`,
            );
            continue;
          }
          if (!matchesAny(facts.providerEventType, providerGuard)) {
            await skip(
              'provider_event_guard',
              `provider_event_type is ${facts.providerEventType}, not ${providerGuard.join(' or ')}`,
            );
            continue;
          }
        }

        if (operation === 'delegateTicket') {
          const phaseGuard = splitList(this.getNodeParameter('phaseGuard', i, ''));
          if (phaseGuard.length && !matchesAny(facts.phase, phaseGuard)) {
            await skip('phase_guard', `phase is ${facts.phase || '(none)'}, not ${phaseGuard.join(' or ')}`);
            continue;
          }
        }

        if (!facts.repo && !facts.boardId) {
          throw new NodeOperationError(
            this.getNode(),
            'the item carries neither data.repo nor a board id, so no fleet agent can be resolved',
            { itemIndex: i },
          );
        }

        const registryPath = hermesRegistryPath(this.getNodeParameter('registryFile', i, ''));
        if (!registry || registry.path !== registryPath) {
          try {
            registry = { path: registryPath, value: await loadHermesRegistry(registryPath) };
          } catch (error) {
            throw new NodeOperationError(
              this.getNode(),
              `Cannot read the fleet registry at ${registryPath}: ${(error as Error).message}`,
              { itemIndex: i },
            );
          }
        }
        const route = resolveFleetAgentForBoard(registry.value, facts.boardId, facts.repo);

        if (!route.eligible) {
          if (this.getNodeParameter('onIneligible', i, 'skip') === 'error' && route.code !== 'no_route') {
            throw new NodeOperationError(this.getNode(), route.why, { itemIndex: i });
          }
          await skip(route.code || 'ineligible', route.why, route);
          continue;
        }

        // Read canonical enrollment every dispatch; never trust a stale copy.
        // Evaluated after eligibility so a switched-off project reports that,
        // not a fence it would never have reached.
        if (route.projectPath) {
          const mode = await executionMode(route.projectPath);
          if (mode !== 'legacy') {
            await skip(
              'fenced',
              `Krebs owns ${mode} execution for ${route.agentId}; legacy fleet dispatch is fenced`,
              route,
            );
            continue;
          }
        }

        let prompt: string;
        if (operation === 'groomTicket') {
          const label = optionalText(this.getNodeParameter('groomedLabel', i, GROOMED_LABEL)) || '';
          prompt = groomingPrompt(facts, route.projectPath, label);
        } else if (operation === 'delegateTicket') {
          const label = optionalText(this.getNodeParameter('requiredLabel', i, GROOMED_LABEL)) || '';
          prompt = delegationPrompt(facts, route.projectPath, label);
        } else if (operation === 'invoke') {
          prompt = nonblank(this.getNodeParameter('prompt', i), 'prompt');
        } else {
          throw new NodeOperationError(
            this.getNode(),
            `Unknown 33GOD Agent Fleet operation: ${operation}`,
            { itemIndex: i },
          );
        }

        // The ticket is the conversation: correlation comes from the causing
        // envelope, else from the ticket itself. Idempotency is separate: the
        // command id is derived from the causing event id, so a redelivered
        // trigger event republishes the same command_id and idempotency_key,
        // and `observedAt` (the causing event's time) makes the whole envelope
        // byte-identical, which is what the gateway's journal keys on (command_id
        // plus an envelope digest): the duplicate is recognised, not rejected.
        const commandId = fleetCommandId(causationId, operation, route.agentId);
        const result = await send({
          type: INVOCATION_COMMAND_TYPE,
          kind: 'command',
          data: {
            target_agent_id: route.agentId,
            prompt,
            context: {
              reason: reasonForInvocation,
              repo: facts.repo,
              ticket_key: facts.ticketKey,
              ticket_id: facts.ticketId,
              board_id: facts.boardId,
              workspace: facts.workspace,
              title: facts.title,
              phase: facts.phase,
              previous_phase: facts.previousPhase,
              provider_event_type: facts.providerEventType,
            },
          },
          commandId,
          eventId: commandId,
          correlationId,
          causationId,
          observedAt,
          source: FLEET_SOURCE,
          host: conn.natsHost || undefined,
          port: conn.natsPort ? Number(conn.natsPort) : undefined,
          timeoutMs: conn.timeoutMs ? Number(conn.timeoutMs) : undefined,
          producer: 'n8n',
          service,
        });

        dispatched.push({
          json: {
            invoked: true,
            skipped: false,
            reason: reasonForInvocation,
            ...ticketJson,
            subject: result.subject,
            commandId: result.commandId ?? null,
            eventId: result.eventId,
            correlationid: result.correlationid,
            agentId: route.agentId,
            profileName: route.profileName ?? null,
            projectPath: route.projectPath ?? null,
            matchedBy: route.matchedBy,
          },
          pairedItem: { item: i },
        });
      } catch (error) {
        if (this.continueOnFail()) {
          skipped.push({
            json: { invoked: false, skipped: true, code: 'error', error: (error as Error).message },
            pairedItem: { item: i },
          });
          continue;
        }
        throw error;
      }
    }

    return [dispatched, skipped];
  }
}
