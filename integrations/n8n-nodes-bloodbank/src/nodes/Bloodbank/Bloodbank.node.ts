import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { publish } from '../../nats';
import { eventSchemas } from './eventSchemas';

function eventOptions(): NonNullable<INodeProperties['options']> {
  return eventSchemas.map((e) => {
    const req = e.dataFields.filter((f) => f.required).map((f) => f.name);
    const note = req.length ? ` — data requires: ${req.join(', ')}` : '';
    return {
      name: e.type,
      value: e.type,
      description: (e.description || e.title) + note,
    };
  });
}

export class Bloodbank implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Bloodbank',
    name: 'bloodbank',
    group: ['output'],
    version: 1,
    subtitle: '={{$parameter["event"]}}',
    description: 'Publish a canonical event to the 33GOD Bloodbank NATS bus',
    defaults: { name: 'Bloodbank' },
    inputs: ['main'],
    outputs: ['main'],
    usableAsTool: true,
    properties: [
      {
        displayName: 'Event',
        name: 'event',
        type: 'options',
        noDataExpression: true,
        options: eventOptions(),
        default: eventSchemas.length ? eventSchemas[0].type : '',
        required: true,
        description:
          'The Bloodbank event to publish (generated from schemas intersected with publisher-events.json)',
      },
      {
        displayName: 'Data (JSON)',
        name: 'data',
        type: 'json',
        default: '{}',
        description:
          'Event payload object. Required fields per event are shown in the Event dropdown. Provide literal JSON or an expression returning an object.',
      },
      {
        displayName: 'Connection',
        name: 'connection',
        type: 'collection',
        placeholder: 'Add option',
        default: {},
        options: [
          { displayName: 'NATS Host', name: 'natsHost', type: 'string', default: '127.0.0.1' },
          { displayName: 'NATS Port', name: 'natsPort', type: 'number', default: 4222 },
        ],
      },
      {
        displayName: 'Envelope Metadata',
        name: 'metadata',
        type: 'collection',
        placeholder: 'Add metadata',
        default: {},
        options: [
          {
            displayName: 'Actor ID',
            name: 'actorId',
            type: 'string',
            default: 'bloodbank.integration.n8n',
          },
          {
            displayName: 'Actor Type',
            name: 'actorType',
            type: 'string',
            default: 'service',
          },
          {
            displayName: 'Causation ID',
            name: 'causationId',
            type: 'string',
            default: '',
            description: 'CloudEvent ID that directly caused this event; defaults to Event ID for a root event',
          },
          {
            displayName: 'Correlation ID',
            name: 'correlationId',
            type: 'string',
            default: '',
          },
          {
            displayName: 'Event ID',
            name: 'eventId',
            type: 'string',
            default: '',
            description: 'Stable UUID to preserve across retries; generated for this publish when omitted',
          },
          {
            displayName: 'Observed At',
            name: 'observedAt',
            type: 'string',
            default: '',
            description: 'RFC 3339 source observation time; current time when omitted',
          },
          {
            displayName: 'Ordering Key',
            name: 'orderingKey',
            type: 'string',
            default: '',
            description: 'Stable entity ordering key; repo.task events derive task:<repo>:<task_id>',
          },
        ],
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const out: INodeExecutionData[] = [];

    for (let i = 0; i < items.length; i++) {
      try {
        const type = this.getNodeParameter('event', i) as string;
        const rawData = this.getNodeParameter('data', i, {}) as unknown;
        const conn = this.getNodeParameter('connection', i, {}) as {
          natsHost?: string;
          natsPort?: number;
        };
        const metadata = this.getNodeParameter('metadata', i, {}) as {
          actorId?: string;
          actorType?: string;
          causationId?: string;
          correlationId?: string;
          eventId?: string;
          observedAt?: string;
          orderingKey?: string;
        };

        let data: Record<string, unknown>;
        if (typeof rawData === 'string') {
          data = rawData.trim() ? (JSON.parse(rawData) as Record<string, unknown>) : {};
        } else {
          data = (rawData || {}) as Record<string, unknown>;
        }

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

        const res = await publish({
          type,
          data,
          host: conn.natsHost || undefined,
          port: conn.natsPort ? Number(conn.natsPort) : undefined,
          producer: 'n8n',
          service: 'n8n',
          eventId: metadata.eventId?.trim() || undefined,
          observedAt: metadata.observedAt?.trim() || undefined,
          correlationId: metadata.correlationId?.trim() || undefined,
          causationId: metadata.causationId?.trim() || undefined,
          orderingKey: metadata.orderingKey?.trim() || undefined,
          actor: {
            type: metadata.actorType?.trim() || 'service',
            agent_id: metadata.actorId?.trim() || 'bloodbank.integration.n8n',
          },
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
