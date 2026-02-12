import { config } from './config';

const MAX_RETRIES = 3;
const BASE_DELAY_MS = 200;

// Simple token-bucket rate limiter
let tokens = config.maxEventsPerSecond;
let lastRefill = Date.now();

function refillTokens() {
  const now = Date.now();
  const elapsed = now - lastRefill;
  const refill = (elapsed / 1000) * config.maxEventsPerSecond;
  tokens = Math.min(config.maxEventsPerSecond, tokens + refill);
  lastRefill = now;
}

async function waitForToken(): Promise<void> {
  refillTokens();
  if (tokens >= 1) {
    tokens -= 1;
    return;
  }
  // Wait until a token is available
  const waitMs = ((1 - tokens) / config.maxEventsPerSecond) * 1000;
  await new Promise((resolve) => setTimeout(resolve, Math.ceil(waitMs)));
  refillTokens();
  tokens -= 1;
}

/**
 * Publish an event to Bloodbank HTTP API with retry logic.
 * Gracefully degrades if Bloodbank is unavailable.
 */
export async function publishEvent(
  agentName: string,
  action: string,
  payload: Record<string, unknown>,
): Promise<boolean> {
  await waitForToken();

  const url = `${config.bloodbankUrl}/events/agent/${agentName}/${action}`;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...payload,
          _bridge: {
            agent: agentName,
            action,
            published_at: new Date().toISOString(),
          },
        }),
        signal: AbortSignal.timeout(5000),
      });

      if (response.ok) {
        return true;
      }

      // Don't retry on 4xx (client errors)
      if (response.status >= 400 && response.status < 500) {
        console.warn(
          `[publisher] ${action} → ${response.status} (not retrying): ${await response.text().catch(() => '')}`,
        );
        return false;
      }

      // Server error — retry
      console.warn(
        `[publisher] ${action} → ${response.status} (attempt ${attempt + 1}/${MAX_RETRIES + 1})`,
      );
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);

      if (attempt === MAX_RETRIES) {
        // Final attempt failed — log and move on
        console.error(`[publisher] ${action} failed after ${MAX_RETRIES + 1} attempts: ${errMsg}`);
        return false;
      }

      // Connection error — retry with backoff
      console.warn(
        `[publisher] ${action} connection error (attempt ${attempt + 1}/${MAX_RETRIES + 1}): ${errMsg}`,
      );
    }

    // Exponential backoff
    const delay = BASE_DELAY_MS * Math.pow(2, attempt);
    await new Promise((resolve) => setTimeout(resolve, delay));
  }

  return false;
}
