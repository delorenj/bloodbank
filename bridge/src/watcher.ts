import * as fs from 'fs';
import * as path from 'path';
import { config } from './config';
import { parseLine, type BridgeEvent } from './parser';
import { publishEvent } from './publisher';

/**
 * Track byte offsets per file so we only read new content.
 */
const fileOffsets = new Map<string, number>();

/**
 * Active fs.FSWatcher instances for cleanup.
 */
const watchers: fs.FSWatcher[] = [];

/**
 * Debounce timers per file to batch rapid changes.
 */
const debounceTimers = new Map<string, ReturnType<typeof setTimeout>>();
const DEBOUNCE_MS = 100;

/**
 * Check if a filename is a valid session JSONL file.
 */
function isSessionFile(filename: string): boolean {
  return filename.endsWith('.jsonl') && !filename.includes('.deleted') && !filename.includes('.lock');
}

/**
 * Extract agent ID from a session directory path.
 * Path pattern: ~/.openclaw/agents/{agentId}/sessions/
 */
function extractAgentId(dirPath: string): string | null {
  const match = dirPath.match(/\/agents\/([^/]+)\/sessions\/?$/);
  return match ? match[1] : null;
}

/**
 * Read new lines from a file starting at the tracked offset.
 */
function readNewLines(filePath: string): string[] {
  let offset = fileOffsets.get(filePath) ?? 0;

  let stat: fs.Stats;
  try {
    stat = fs.statSync(filePath);
  } catch {
    return [];
  }

  // File was truncated or replaced — reset
  if (stat.size < offset) {
    offset = 0;
  }

  if (stat.size === offset) {
    return [];
  }

  const fd = fs.openSync(filePath, 'r');
  const buffer = Buffer.alloc(stat.size - offset);
  fs.readSync(fd, buffer, 0, buffer.length, offset);
  fs.closeSync(fd);

  fileOffsets.set(filePath, stat.size);

  const chunk = buffer.toString('utf-8');
  return chunk.split('\n').filter((l) => l.trim().length > 0);
}

/**
 * Initialize file offset to end-of-file (tail-only mode).
 */
function initializeOffset(filePath: string): void {
  try {
    const stat = fs.statSync(filePath);
    fileOffsets.set(filePath, stat.size);
  } catch {
    fileOffsets.set(filePath, 0);
  }
}

/**
 * Process new lines from a file: parse and publish events.
 */
async function processFileChange(dirPath: string, filename: string): Promise<void> {
  if (!isSessionFile(filename)) return;

  const filePath = path.join(dirPath, filename);
  const agentId = extractAgentId(dirPath);
  if (!agentId) {
    console.warn(`[watcher] Could not extract agent ID from: ${dirPath}`);
    return;
  }

  const agentName = config.agentNames[agentId] || agentId;
  const sessionId = path.basename(filename, '.jsonl');

  // Initialize offset for new files
  if (!fileOffsets.has(filePath)) {
    if (config.tailOnly) {
      initializeOffset(filePath);
      console.log(`[watcher] Tracking new file: ${filename} (agent: ${agentName})`);
      return; // Don't process existing content
    }
  }

  const lines = readNewLines(filePath);
  if (lines.length === 0) return;

  let eventCount = 0;
  for (const line of lines) {
    let events: BridgeEvent[];
    try {
      events = parseLine(line, agentId, agentName, sessionId);
    } catch (err) {
      console.error(`[watcher] Parse error: ${err}`);
      continue;
    }

    for (const event of events) {
      eventCount++;
      publishEvent(agentName, event.action, event.payload).catch((err) => {
        console.error(`[watcher] Publish error: ${err}`);
      });
    }
  }

  if (eventCount > 0) {
    console.log(`[watcher] ${agentName}: ${eventCount} event(s) from ${filename}`);
  }
}

/**
 * Initialize tracking for all existing JSONL files in a directory.
 */
function initExistingFiles(dirPath: string): void {
  try {
    const files = fs.readdirSync(dirPath);
    let tracked = 0;
    for (const file of files) {
      if (isSessionFile(file)) {
        const filePath = path.join(dirPath, file);
        if (config.tailOnly) {
          initializeOffset(filePath);
        }
        tracked++;
      }
    }
    const agentId = extractAgentId(dirPath);
    const agentName = agentId ? config.agentNames[agentId] || agentId : 'unknown';
    console.log(`[watcher] ${agentName}: tracking ${tracked} existing session file(s)`);
  } catch {
    // Directory may not exist yet — that's fine
  }
}

/**
 * Watch a single session directory using native fs.watch.
 */
function watchDirectory(dirPath: string): void {
  const normalized = dirPath.endsWith('/') ? dirPath : dirPath + '/';

  // Ensure directory exists
  try {
    fs.mkdirSync(normalized, { recursive: true });
  } catch {
    // Already exists
  }

  // Track existing files
  initExistingFiles(normalized);

  try {
    const watcher = fs.watch(normalized, (eventType, filename) => {
      if (!filename || eventType !== 'change') return;

      // Debounce rapid changes to the same file
      const key = path.join(normalized, filename);
      const existing = debounceTimers.get(key);
      if (existing) clearTimeout(existing);

      debounceTimers.set(
        key,
        setTimeout(() => {
          debounceTimers.delete(key);
          processFileChange(normalized, filename).catch((err) => {
            console.error(`[watcher] Error processing ${filename}: ${err}`);
          });
        }, DEBOUNCE_MS),
      );
    });

    watchers.push(watcher);
    console.log(`[watcher] Watching: ${normalized}`);
  } catch (err) {
    console.error(`[watcher] Failed to watch ${normalized}: ${err}`);
  }
}

/**
 * Start watching all configured session directories.
 */
export function startWatchers(): void {
  for (const dir of config.sessionDirs) {
    watchDirectory(dir);
  }

  console.log(`[watcher] Ready. Listening for new JSONL lines...`);

  // Graceful shutdown
  const cleanup = () => {
    console.log('\n[watcher] Shutting down...');
    for (const w of watchers) {
      w.close();
    }
    for (const timer of debounceTimers.values()) {
      clearTimeout(timer);
    }
    console.log('[watcher] Closed.');
    process.exit(0);
  };

  process.on('SIGINT', cleanup);
  process.on('SIGTERM', cleanup);
}
