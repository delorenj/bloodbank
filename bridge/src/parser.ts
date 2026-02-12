import { config } from './config';

/**
 * Parsed event ready for publishing to Bloodbank.
 */
export interface BridgeEvent {
  action: string;
  payload: Record<string, unknown>;
}

/**
 * A single content block inside an OpenClaw message.
 */
interface ContentBlock {
  type: string;
  text?: string;
  id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  toolCallId?: string;
  toolName?: string;
  thinking?: string;
}

/**
 * The top-level shape of a JSONL line from OpenClaw.
 */
interface SessionLine {
  type: string;
  customType?: string;
  id?: string;
  parentId?: string | null;
  timestamp?: string;
  message?: {
    role: string;
    content: ContentBlock[] | string;
    model?: string;
    provider?: string;
    usage?: Record<string, unknown>;
    stopReason?: string;
    timestamp?: number;
    toolCallId?: string;
    toolName?: string;
  };
  data?: Record<string, unknown>;
}

function truncate(text: string, max: number = config.maxPreviewLength): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + '…';
}

function extractText(content: ContentBlock[] | string): string {
  if (typeof content === 'string') return content;
  return content
    .filter((c) => c.type === 'text' && c.text)
    .map((c) => c.text!)
    .join(' ');
}

function isHeartbeatPoll(text: string): boolean {
  return text.includes('Read HEARTBEAT.md if it exists');
}

function isHeartbeatOkResponse(content: ContentBlock[] | string): boolean {
  const text = extractText(content);
  return text.trim() === 'HEARTBEAT_OK' || text.trim().startsWith('HEARTBEAT_OK');
}

/**
 * Parse a single JSONL line into zero or more BridgeEvents.
 *
 * Each event's payload matches the Bloodbank Pydantic model for that action.
 * The agentName is injected by the caller — we use a placeholder here.
 */
export function parseLine(raw: string, agentId: string, agentName: string, sessionId: string): BridgeEvent[] {
  let line: SessionLine;
  try {
    line = JSON.parse(raw);
  } catch {
    return [];
  }

  const events: BridgeEvent[] = [];
  const sessionKey = `agent:${agentId}:${sessionId}`;

  // Handle session start events
  if (line.type === 'session') {
    events.push({
      action: 'session.started',
      payload: {
        agent_name: agentName,
        session_key: sessionKey,
        model: null,
        channel: null,
      },
    });
    return events;
  }

  // Only care about "message" type lines for the rest
  if (line.type !== 'message' || !line.message) {
    return events;
  }

  const { role, content } = line.message;
  const contentBlocks: ContentBlock[] = Array.isArray(content)
    ? content
    : typeof content === 'string'
      ? [{ type: 'text', text: content }]
      : [];

  switch (role) {
    case 'user': {
      const textContent = extractText(content);

      // Skip heartbeat polls (too noisy)
      if (isHeartbeatPoll(textContent)) {
        return events;
      }

      events.push({
        action: 'message.received',
        payload: {
          agent_name: agentName,
          channel: 'openclaw',
          sender: 'user',
          message_preview: truncate(textContent),
          message_length: textContent.length,
          session_key: sessionKey,
        },
      });
      break;
    }

    case 'assistant': {
      // Check for heartbeat OK responses
      if (isHeartbeatOkResponse(content)) {
        events.push({
          action: 'heartbeat',
          payload: {
            agent_name: agentName,
            status: 'ok',
            active_sessions: null,
            uptime_ms: null,
          },
        });
        return events;
      }

      // Extract tool calls
      const toolCalls = contentBlocks.filter((c) => c.type === 'toolCall');
      for (const tc of toolCalls) {
        if (tc.name === 'sessions_spawn') {
          const args = tc.arguments || {};
          events.push({
            action: 'subagent.spawned',
            payload: {
              agent_name: agentName,
              child_label: String(args.label || 'unknown'),
              child_session_key: `agent:${agentId}:subagent:pending`,
              task_preview: truncate(String(args.task || args.message || '')),
              model: args.model || null,
            },
          });
        } else {
          events.push({
            action: 'tool.invoked',
            payload: {
              agent_name: agentName,
              tool_name: tc.name || 'unknown',
              tool_params_preview: truncate(JSON.stringify(tc.arguments || {})),
              session_key: sessionKey,
            },
          });
        }
      }

      // Extract text response
      const textContent = extractText(content);
      if (textContent.trim()) {
        const usage = line.message.usage as Record<string, unknown> | undefined;
        const totalTokens = usage?.totalTokens ?? usage?.total_tokens ?? null;

        events.push({
          action: 'message.sent',
          payload: {
            agent_name: agentName,
            channel: 'openclaw',
            message_preview: truncate(textContent),
            message_length: textContent.length,
            model: line.message.model || null,
            tokens_used: totalTokens,
            duration_ms: null,
          },
        });
      }
      break;
    }

    case 'toolResult': {
      const msg = line.message;
      const resultText = extractText(content);
      const toolName = msg.toolName || 'unknown';

      if (toolName === 'sessions_spawn' || toolName === 'sessions_send') {
        events.push({
          action: 'subagent.completed',
          payload: {
            agent_name: agentName,
            child_label: toolName === 'sessions_spawn' ? 'spawned' : 'sent',
            child_session_key: sessionKey,
            success: true,
            duration_ms: null,
            result_preview: truncate(resultText),
          },
        });
      } else {
        events.push({
          action: 'tool.completed',
          payload: {
            agent_name: agentName,
            tool_name: toolName,
            success: true,
            duration_ms: null,
            output_preview: truncate(resultText),
          },
        });
      }
      break;
    }
  }

  return events;
}
