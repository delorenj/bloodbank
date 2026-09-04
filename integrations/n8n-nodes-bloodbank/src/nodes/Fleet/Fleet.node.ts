import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';
import { parse as parseYaml } from 'yaml';

import {
  delegationPrompt,
  groomingPrompt,
  resolveFleetAgentForBoard,
  ticketCorrelationId,
  ticketFactsFromEnvelope,
} from '../../fleet';
import type { TicketFacts } from '../../fleet';
import { publish } from '../../nats';

const INVOCATION_COMMAND_TYPE = 'bloodbank.agent.invocation.start';
const DEFAULT_REGISTRY = '~/.hermes/agents-registry.yaml';
const GROOMED_LABEL = 'lifecycle:triaged';

function expandHome(path: string): string {
  if (path === '~') return homedir();
  if (path.startsWith('~/')) return `${homedir()}/${path.slice(2)}`;
  return path;
}

function nonblank(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value.trim();
}

function optionalText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

function jsonObject(value: unknown, name: string): Record<string, unknown> {
  let parsed = value;
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

function listValues(value: unknown): string[] {
  return String(value ?? '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

/** Case-insensitive membership, so a guard can name a state or its group. */
function matchesAny(candidate: string, allowed: string[]): boolean {
  if (!allowed.length) return true;
  const needle = candidate.trim().toLowerCase();
  return allowed.some((entry) => entry.toLowerCase() === needle);
}

export class Fleet implements INodeType {
  description: INodeTypeDescription = {
    displayName: '33GOD Agent Fleet',
    name: 'bloodbankFleet',
    icon: { light: 'file:fleet.svg', dark: 'file:fleet.dark.svg' },
    group: ['output'],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description:
      'Hand a ticket to the fleet agent that owns its board — registry-resolved, one thread per ticket, ineligible projects skipped',
    defaults: { name: '33GOD Agent Fleet' },
    inputs: ['main'],
    outputs: ['main'],
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
        type: 'string',
        default: '',
        placeholder: 'plane.ticket.created',
        description:
          'Skip the item unless data.provider_event_type matches. Leave empty to accept any. One shared trigger can then feed several operations.',
      },
      {
        displayName: 'On Ineligible Agent',
        name: 'onIneligible',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Skip',
            value: 'skip',
            description:
              'Report the reason and publish nothing. A switched-off project stays a green execution.',
          },
          { name: 'Error', value: 'error', description: 'Fail the item' },
        ],
        default: 'skip',
        description:
          'What to do when the registry route exists but bloodbank.enabled, gateway_scope or target_agent_id says the bus may not address it',
      },
      {
        displayName: 'Ticket',
        name: 'ticket',
        type: 'collection',
        placeholder: 'Add field',
        default: {},
        description:
          'Overrides for the incoming item. By default every field is lifted from the Bloodbank envelope on the input, so a trigger can feed this node with no mapping at all.',
        options: [
          {
            displayName: 'Ticket Event (JSON)',
            name: 'event',
            type: 'json',
            default: '',
            description:
              'A whole Bloodbank envelope or bare data object to read instead of the input item',
          },
          { displayName: 'Repository', name: 'repo', type: 'string', default: '' },
          { displayName: 'Board ID', name: 'boardId', type: 'string', default: '' },
          { displayName: 'Ticket Key', name: 'ticketKey', type: 'string', default: '' },
          { displayName: 'Title', name: 'title', type: 'string', default: '' },
        ],
      },
      {
        displayName: 'Hermes Registry File',
        name: 'registryFile',
        type: 'string',
        default: DEFAULT_REGISTRY,
        required: true,
        description: 'Canonical fleet registry, re-read on every execution',
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
    ],
  };

  async execute(
    this: IExecuteFunctions,
    publishMessage?: unknown,
  ): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const out: INodeExecutionData[] = [];
    const send = typeof publishMessage === 'function' ? (publishMessage as typeof publish) : publish;

    for (let i = 0; i < items.length; i++) {
      try {
        const operation = this.getNodeParameter('operation', i) as string;
        const overrides = jsonObject(this.getNodeParameter('ticket', i, {}), 'ticket overrides');
        const conn = this.getNodeParameter('connection', i, {}) as {
          natsHost?: string;
          natsPort?: number;
          timeoutMs?: number;
        };

        const declared = optionalText(overrides.event);
        const envelope = declared
          ? jsonObject(declared, 'ticket event')
          : ((items[i].json || {}) as Record<string, unknown>);
        const lifted = ticketFactsFromEnvelope(envelope);
        const facts: TicketFacts = {
          ...lifted,
          repo: optionalText(overrides.repo) || lifted.repo,
          boardId: optionalText(overrides.boardId) || lifted.boardId,
          ticketKey: optionalText(overrides.ticketKey) || lifted.ticketKey,
          title: optionalText(overrides.title) || lifted.title,
        };

        const providerGuard = listValues(this.getNodeParameter('providerEventGuard', i, ''));
        if (facts.providerEventType && !matchesAny(facts.providerEventType, providerGuard)) {
          out.push({
            json: {
              invoked: false,
              skipped: true,
              reason: `provider_event_type is ${facts.providerEventType}, not ${providerGuard.join(' or ')}`,
              operation,
              ticketKey: facts.ticketKey,
            },
            pairedItem: { item: i },
          });
          continue;
        }

        if (operation === 'delegateTicket') {
          const phaseGuard = listValues(this.getNodeParameter('phaseGuard', i, ''));
          if (phaseGuard.length && !matchesAny(facts.phase, phaseGuard)) {
            out.push({
              json: {
                invoked: false,
                skipped: true,
                reason: `phase is ${facts.phase || '(none)'}, not ${phaseGuard.join(' or ')}`,
                operation,
                ticketKey: facts.ticketKey,
              },
              pairedItem: { item: i },
            });
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

        const registryPath = expandHome(
          nonblank(this.getNodeParameter('registryFile', i), 'fleet registry path'),
        );
        let registry: unknown;
        try {
          registry = parseYaml(await readFile(registryPath, 'utf8'));
        } catch (error) {
          throw new NodeOperationError(
            this.getNode(),
            `Cannot read the fleet registry at ${registryPath}: ${(error as Error).message}`,
            { itemIndex: i },
          );
        }
        const route = resolveFleetAgentForBoard(registry, facts.boardId, facts.repo);

        if (!route.eligible) {
          const onIneligible = this.getNodeParameter('onIneligible', i, 'skip') as string;
          if (onIneligible === 'error') {
            throw new NodeOperationError(this.getNode(), route.why, { itemIndex: i });
          }
          out.push({
            json: {
              invoked: false,
              skipped: true,
              reason: route.why,
              operation,
              agentId: route.agentId,
              matchedBy: route.matchedBy,
              ticketKey: facts.ticketKey,
              boardId: facts.boardId,
            },
            pairedItem: { item: i },
          });
          continue;
        }

        let prompt: string;
        let reason: string;
        if (operation === 'groomTicket') {
          const label = optionalText(this.getNodeParameter('groomedLabel', i, GROOMED_LABEL)) || '';
          prompt = groomingPrompt(facts, route.projectPath, label);
          reason = 'ticket-grooming';
        } else if (operation === 'delegateTicket') {
          const label = optionalText(this.getNodeParameter('requiredLabel', i, GROOMED_LABEL)) || '';
          prompt = delegationPrompt(facts, route.projectPath, label);
          reason = 'ticket-delegation';
        } else if (operation === 'invoke') {
          prompt = nonblank(this.getNodeParameter('prompt', i), 'prompt');
          reason = 'fleet-invoke';
        } else {
          throw new NodeOperationError(
            this.getNode(),
            `Unknown 33GOD Agent Fleet operation: ${operation}`,
            { itemIndex: i },
          );
        }

        // A command's correlation id defaults to its own command id, which would
        // give every invocation its own thread. Derive it from the ticket so the
        // ticket is the conversation, and a redelivered webhook lands on the same
        // idempotency key instead of starting a second one.
        const inherited = optionalText((envelope as Record<string, unknown>).correlationid);
        const correlationId =
          inherited || ticketCorrelationId(facts.boardId, facts.ticketKey || facts.ticketId);
        const causationId = optionalText((envelope as Record<string, unknown>).id);

        const result = await send({
          type: INVOCATION_COMMAND_TYPE,
          kind: 'command',
          data: {
            target_agent_id: route.agentId,
            prompt,
            context: {
              reason,
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
          correlationId,
          causationId,
          host: conn.natsHost || undefined,
          port: conn.natsPort ? Number(conn.natsPort) : undefined,
          timeoutMs: conn.timeoutMs ? Number(conn.timeoutMs) : undefined,
          producer: 'n8n',
          service: optionalText(this.getNodeParameter('service', i, '')) || 'n8n-agent-fleet',
        });

        out.push({
          json: {
            invoked: true,
            skipped: false,
            operation,
            reason,
            subject: result.subject,
            commandId: result.commandId,
            eventId: result.eventId,
            correlationid: result.correlationid,
            agentId: route.agentId,
            profileName: route.profileName,
            projectPath: route.projectPath,
            matchedBy: route.matchedBy,
            repo: facts.repo,
            boardId: facts.boardId,
            ticketKey: facts.ticketKey,
          },
          pairedItem: { item: i },
        });
      } catch (error) {
        if (this.continueOnFail()) {
          out.push({
            json: { invoked: false, error: (error as Error).message },
            pairedItem: { item: i },
          });
          continue;
        }
        throw error;
      }
    }

    return [out];
  }
}
