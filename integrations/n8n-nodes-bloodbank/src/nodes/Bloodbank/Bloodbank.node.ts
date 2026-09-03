import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';
import { parse as parseYaml } from 'yaml';

import { publish } from '../../nats';
import { resolveFleetTargetForRepo } from '../../registry';
import { commandSchemas, eventSchemas } from './eventSchemas';

const INVOCATION_COMMAND_TYPE = 'bloodbank.agent.invocation.start';

function schemaOptions(schemas: typeof eventSchemas): NonNullable<INodeProperties['options']> {
  return schemas.map((schema) => {
    const required = schema.dataFields.filter((field) => field.required).map((field) => field.name);
    const note = required.length ? ` — data requires: ${required.join(', ')}` : '';
    return {
      name: schema.type,
      value: schema.type,
      description: (schema.description || schema.title) + note,
    };
  });
}

function eventOptions(): NonNullable<INodeProperties['options']> {
  return schemaOptions(eventSchemas);
}

function commandOptions(): NonNullable<INodeProperties['options']> {
  return schemaOptions(
    commandSchemas.filter((schema) => schema.type === INVOCATION_COMMAND_TYPE),
  );
}

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

function nullableJsonObject(value: unknown, name: string): Record<string, unknown> | null {
  if (value === null || (typeof value === 'string' && value.trim() === 'null')) return null;
  return jsonObject(value, name);
}

export class Bloodbank implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Bloodbank',
    name: 'bloodbank',
    icon: { light: 'file:bloodbank.svg', dark: 'file:bloodbank.dark.svg' },
    group: ['output'],
    version: 1,
    subtitle: '={{$parameter["mode"] === "command" ? $parameter["command"] : $parameter["event"]}}',
    description: 'Publish a schema-validated event or invocation command to the 33GOD Bloodbank NATS bus',
    defaults: { name: 'Bloodbank' },
    inputs: ['main'],
    outputs: ['main'],
    usableAsTool: true,
    properties: [
      {
        displayName: 'Mode',
        name: 'mode',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Event',
            value: 'event',
            description: 'Publish an event payload exactly as before',
          },
          {
            name: 'Command',
            value: 'command',
            description: 'Publish one registry-routed agent invocation command',
          },
        ],
        default: 'event',
        required: true,
      },
      {
        displayName: 'Event',
        name: 'event',
        type: 'options',
        noDataExpression: true,
        options: eventOptions(),
        default: eventSchemas.length ? eventSchemas[0].type : '',
        required: true,
        displayOptions: { show: { mode: ['event'] } },
        description: 'The bloodbank event to publish (generated from schemas/bloodbank/**)',
      },
      {
        displayName: 'Data (JSON)',
        name: 'data',
        type: 'json',
        default: '{}',
        displayOptions: { show: { mode: ['event'] } },
        description:
          'Event payload object. Required fields per event are shown in the Event dropdown. Provide literal JSON or an expression returning an object.',
      },
      {
        displayName: 'Command',
        name: 'command',
        type: 'options',
        noDataExpression: true,
        options: commandOptions(),
        default: INVOCATION_COMMAND_TYPE,
        required: true,
        displayOptions: { show: { mode: ['command'] } },
        description: 'The canonical invocation command generated from schemas/bloodbank/**',
      },
      {
        displayName: 'Repository',
        name: 'repository',
        type: 'string',
        default: '',
        required: true,
        displayOptions: { show: { mode: ['command'] } },
        description: 'Repository name used to resolve exactly one eligible target from the fleet registry',
      },
      {
        displayName: 'Prompt',
        name: 'prompt',
        type: 'string',
        typeOptions: { rows: 5 },
        default: '',
        required: true,
        displayOptions: { show: { mode: ['command'] } },
        description: 'Non-empty instruction for the resolved fleet agent',
      },
      {
        displayName: 'Context (JSON)',
        name: 'context',
        type: 'json',
        default: '{}',
        displayOptions: { show: { mode: ['command'] } },
        description: 'Optional structured context for the invocation',
      },
      {
        displayName: 'Thread ID',
        name: 'threadId',
        type: 'string',
        default: '',
        displayOptions: { show: { mode: ['command'] } },
        description: 'Optional existing thread identifier for continuation',
      },
      {
        displayName: 'Turn ID',
        name: 'turnId',
        type: 'string',
        default: '',
        displayOptions: { show: { mode: ['command'] } },
        description: 'Optional caller-assigned turn identifier',
      },
      {
        displayName: 'Command Identity',
        name: 'identity',
        type: 'collection',
        placeholder: 'Add identity field',
        default: {},
        displayOptions: { show: { mode: ['command'] } },
        options: [
          {
            displayName: 'Command ID',
            name: 'commandId',
            type: 'string',
            default: '',
            required: true,
            description: 'Retry-stable UUID for this command; reused as the root correlation ID',
          },
          {
            displayName: 'Correlation ID',
            name: 'correlationId',
            type: 'string',
            default: '',
            description: 'Optional existing correlation UUID for a non-root command',
          },
          {
            displayName: 'Causation ID',
            name: 'causationId',
            type: 'string',
            default: '',
            description: 'Optional UUID of the message that caused this command',
          },
        ],
      },
      {
        displayName: 'Hermes Registry File',
        name: 'registryFile',
        type: 'string',
        default: '~/.hermes/agents-registry.yaml',
        required: true,
        displayOptions: { show: { mode: ['command'] } },
        description: 'Canonical fleet registry read on every command execution',
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
    ],
  };

  async execute(
    this: IExecuteFunctions,
    publishMessage?: unknown,
  ): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const out: INodeExecutionData[] = [];
    const send = typeof publishMessage === 'function'
      ? publishMessage as typeof publish
      : publish;

    for (let i = 0; i < items.length; i++) {
      try {
        const mode = this.getNodeParameter('mode', i, 'event') as string;
        const conn = this.getNodeParameter('connection', i, {}) as {
          natsHost?: string;
          natsPort?: number;
          timeoutMs?: number;
        };

        if (mode === 'command') {
          const type = this.getNodeParameter('command', i) as string;
          if (type !== INVOCATION_COMMAND_TYPE || !commandSchemas.some((schema) => schema.type === type)) {
            throw new NodeOperationError(this.getNode(), `Unknown Bloodbank command: ${type}`, {
              itemIndex: i,
            });
          }
          const repository = nonblank(this.getNodeParameter('repository', i), 'repository');
          const prompt = nonblank(this.getNodeParameter('prompt', i), 'prompt');
          const context = nullableJsonObject(this.getNodeParameter('context', i, {}), 'context');
          const identity = jsonObject(
            this.getNodeParameter('identity', i, {}),
            'command identity',
          );
          const commandId = nonblank(identity.commandId, 'command ID');
          const registryPath = expandHome(
            nonblank(this.getNodeParameter('registryFile', i), 'fleet registry path'),
          );

          let targetAgentId: string;
          try {
            const registry = parseYaml(await readFile(registryPath, 'utf8'));
            targetAgentId = resolveFleetTargetForRepo(registry, repository);
          } catch (error) {
            throw new NodeOperationError(
              this.getNode(),
              `Cannot resolve command route from ${registryPath}: ${(error as Error).message}`,
              { itemIndex: i },
            );
          }

          const data: Record<string, unknown> = {
            target_agent_id: targetAgentId,
            prompt,
            context,
          };
          const threadId = optionalText(this.getNodeParameter('threadId', i, ''));
          const turnId = optionalText(this.getNodeParameter('turnId', i, ''));
          if (threadId) data.thread_id = threadId;
          if (turnId) data.turn_id = turnId;

          const result = await send({
            type,
            kind: 'command',
            data,
            eventId: commandId,
            commandId,
            correlationId: optionalText(identity.correlationId),
            causationId: optionalText(identity.causationId),
            host: conn.natsHost || undefined,
            port: conn.natsPort ? Number(conn.natsPort) : undefined,
            timeoutMs: conn.timeoutMs ? Number(conn.timeoutMs) : undefined,
            producer: 'n8n',
            service: 'n8n',
          });

          out.push({
            json: {
              published: true,
              kind: 'command',
              type,
              subject: result.subject,
              eventId: result.eventId,
              commandId: result.commandId,
              correlationid: result.correlationid,
              repository,
              targetAgentId,
            },
            pairedItem: { item: i },
          });
          continue;
        }

        if (mode !== 'event') {
          throw new NodeOperationError(this.getNode(), `Unknown Bloodbank publish mode: ${mode}`, {
            itemIndex: i,
          });
        }

        const type = this.getNodeParameter('event', i) as string;
        const data = jsonObject(this.getNodeParameter('data', i, {}), 'data');

        const schema = eventSchemas.find((e) => e.type === type);
        if (!schema) {
          throw new NodeOperationError(this.getNode(), `Unknown bloodbank event: ${type}`, {
            itemIndex: i,
          });
        }
        const missing = schema.dataFields
          .filter((f) => f.required && !(f.name in data))
          .map((f) => f.name);
        if (missing.length) {
          throw new NodeOperationError(
            this.getNode(),
            `Event ${type} is missing required data field(s): ${missing.join(', ')}`,
            { itemIndex: i },
          );
        }

        const res = await send({
          type,
          kind: 'event',
          data,
          host: conn.natsHost || undefined,
          port: conn.natsPort ? Number(conn.natsPort) : undefined,
          timeoutMs: conn.timeoutMs ? Number(conn.timeoutMs) : undefined,
          producer: 'n8n',
          service: 'n8n',
        });

        out.push({
          json: {
            published: true,
            type,
            subject: res.subject,
            eventId: res.eventId,
            correlationid: res.correlationid,
          },
          pairedItem: { item: i },
        });
      } catch (error) {
        if (this.continueOnFail()) {
          out.push({
            json: { published: false, error: (error as Error).message },
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
