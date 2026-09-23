import { readFile } from 'node:fs/promises';

import { parse as parseYaml } from 'yaml';

import { expandHome } from './registry';

/** One Plane board that an enrolled project claims.
 *
 * Project enrollment is canonical in each repo's `.project.json`
 * (`ticket_provider.board_id`); pjangler's registry service indexes those
 * manifests. That is a different — and larger — set than the Hermes org chart,
 * which only lists projects that have an agent. A board belongs on the bus as
 * soon as a project claims it, agent or not.
 */
export interface ProjectBoard {
  boardId: string;
  repo: string;
  workspace?: string;
  identifier?: string;
  projectPath?: string;
  source: 'pjangler' | 'manifest';
}

export const DEFAULT_PROJECT_REGISTRY = 'http://localhost:8764';

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

/** pjangler's own resolution order: explicit, PJ_PROJECT_REGISTRY, PJ_REGISTRY_URL, default. */
export function projectRegistryLocation(explicit?: unknown, env: NodeJS.ProcessEnv = process.env): string {
  return (
    text(explicit) ||
    text(env.PJ_PROJECT_REGISTRY) ||
    text(env.PJ_REGISTRY_URL) ||
    DEFAULT_PROJECT_REGISTRY
  );
}

function planeBinding(provider: unknown): { boardId: string; workspace?: string; identifier?: string } | undefined {
  const binding = record(provider);
  if (!binding) return undefined;
  const type = text(binding.type)?.toLowerCase();
  if (type && type !== 'plane') return undefined;
  const boardId = text(binding.board_id) || text(binding.project_id);
  if (!boardId) return undefined;
  return { boardId, workspace: text(binding.workspace), identifier: text(binding.identifier) };
}

/** Every Plane board claimed by a project in a pjangler registry snapshot. */
export function boardsFromProjectRegistry(value: unknown): ProjectBoard[] {
  const root = record(value) || {};
  const projects = record(root.projects) || {};
  const boards: ProjectBoard[] = [];
  for (const [projectId, raw] of Object.entries(projects)) {
    const project = record(raw);
    if (!project) continue;
    const binding = planeBinding(project.ticket_provider);
    if (!binding) continue;
    boards.push({
      ...binding,
      repo: text(project.slug) || text(project.project_id) || projectId,
      projectPath: text(project.repo_path),
      source: 'pjangler',
    });
  }
  return boards;
}

/** The Plane board one repo's `.project.json` claims, if any. Never throws. */
export async function boardFromManifest(projectPath: string): Promise<ProjectBoard | undefined> {
  let manifest: Record<string, unknown> | undefined;
  try {
    manifest = record(JSON.parse(await readFile(`${projectPath}/.project.json`, 'utf8')));
  } catch {
    return undefined;
  }
  if (!manifest) return undefined;
  const binding = planeBinding(manifest.ticket_provider);
  if (!binding) return undefined;
  const repo = text(manifest.slug) || text(manifest.project_id);
  if (!repo) return undefined;
  return { ...binding, repo, projectPath, source: 'manifest' };
}

export type RegistryFetch = (location: string, timeoutMs: number) => Promise<unknown>;

/** Read a pjangler registry: the service over HTTP, or a fixture file. */
export const fetchProjectRegistry: RegistryFetch = async (location, timeoutMs) => {
  if (/^https?:\/\//i.test(location)) {
    const url = new URL('/v1/registry', location);
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    if (!response.ok) throw new Error(`project registry answered HTTP ${response.status}`);
    return response.json();
  }
  const raw = await readFile(expandHome(location), 'utf8');
  return location.endsWith('.json') ? JSON.parse(raw) : parseYaml(raw);
};

export interface ProjectBoardsResult {
  boards: ProjectBoard[];
  /** fresh: just read. cache: within TTL. stale: read failed, last good served.
   *  unavailable: read failed and nothing was ever read. */
  status: 'fresh' | 'cache' | 'stale' | 'unavailable';
  error?: string;
}

interface CacheEntry {
  location: string;
  boards: ProjectBoard[];
  at: number;
}

let cached: CacheEntry | undefined;

export function clearProjectBoardCache(): void {
  cached = undefined;
}

/** pjangler-enrolled boards, cached briefly and served stale on failure.
 *
 * Every Plane delivery needs the routing table, and the registry service is a
 * local hop, but a restart of that service must not take ticket ingestion down
 * with it: a failed read serves the last good table. With nothing cached the
 * caller carries on with the Hermes registry alone.
 */
export async function loadProjectBoards(
  options: { location?: string; ttlMs?: number; timeoutMs?: number; now?: number; fetcher?: RegistryFetch } = {},
): Promise<ProjectBoardsResult> {
  const location = projectRegistryLocation(options.location);
  const now = options.now ?? Date.now();
  const ttl = options.ttlMs ?? 30_000;
  if (cached && cached.location === location && now - cached.at < ttl) {
    return { boards: cached.boards, status: 'cache' };
  }
  try {
    const value = await (options.fetcher ?? fetchProjectRegistry)(location, options.timeoutMs ?? 1500);
    const boards = boardsFromProjectRegistry(value);
    cached = { location, boards, at: now };
    return { boards, status: 'fresh' };
  } catch (error) {
    const message = (error as Error).message;
    if (cached && cached.location === location) {
      return { boards: cached.boards, status: 'stale', error: message };
    }
    return { boards: [], status: 'unavailable', error: message };
  }
}
