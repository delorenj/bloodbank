import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';

import { parse as parseYaml } from 'yaml';

const CANONICAL_AGENT_ID = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
export const DEFAULT_HERMES_REGISTRY = '~/.hermes/agents-registry.yaml';

function mapping(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function nonblank(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

export function expandHome(path: string): string {
  if (path === '~') return homedir();
  if (path.startsWith('~/')) return `${homedir()}/${path.slice(2)}`;
  return path;
}

/** Where the Hermes org chart lives, without a UI parameter.
 *
 * A saved workflow that still carries the retired `registryFile` parameter
 * wins, silently, so nothing that works today moves. Otherwise the same
 * environment variables the rest of the fleet reads, then the fleet default.
 */
export function hermesRegistryPath(saved?: unknown, env: NodeJS.ProcessEnv = process.env): string {
  return expandHome(
    nonblank(saved) ||
      nonblank(env.HERMES_AGENTS_REGISTRY) ||
      nonblank(env.HERMES_FLEET_REGISTRY_FILE) ||
      DEFAULT_HERMES_REGISTRY,
  );
}

export async function loadHermesRegistry(path: string): Promise<unknown> {
  return parseYaml(await readFile(path, 'utf8'));
}

/** How one registry row's `bloodbank.enabled` resolves.
 *
 * No key means enabled: an ABSENT `enabled` activates the row. Explicit
 * `false` switches it off. Anything else present — `"true"`, `yes` parsed as a
 * string, `null`, `1` — is invalid and treated as off, so a typo never silently
 * widens or narrows dispatch. Mirrors hermes-gateway
 * `contract.registry_bloodbank_enabled`.
 */
export type ActivationState = 'enabled' | 'disabled' | 'invalid';

export function bloodbankActivation(bloodbank: Record<string, unknown>): ActivationState {
  if (!Object.prototype.hasOwnProperty.call(bloodbank, 'enabled')) return 'enabled';
  const value = bloodbank.enabled;
  if (value === true) return 'enabled';
  if (value === false) return 'disabled';
  return 'invalid';
}

export function describeActivation(bloodbank: Record<string, unknown>): string {
  const value = bloodbank.enabled;
  return value === null ? 'null' : `${typeof value} ${JSON.stringify(value)}`;
}

/** Resolve a repository to one registry-authorized fleet target.
 *
 * Profile names are checked only as an eligibility prerequisite. They never
 * leave this boundary: command producers address the returned agent ID.
 */
export function resolveFleetTargetForRepo(registryValue: unknown, repoValue: string): string {
  const repo = nonblank(repoValue);
  if (!repo) throw new Error('repository must be a non-empty string');

  const root = mapping(registryValue);
  if (!root) throw new Error('fleet registry root must be a mapping');
  if (!Number.isInteger(root.schema_version) || root.schema_version !== 1) {
    throw new Error('fleet registry schema_version must be exactly 1');
  }
  const agents = mapping(root.agents);
  if (!agents) throw new Error('fleet registry agents must be a mapping');

  const repositoryRecords: Array<{ agentId: string; entry: Record<string, unknown> }> = [];
  for (const [agentId, rawEntry] of Object.entries(agents)) {
    if (!CANONICAL_AGENT_ID.test(agentId)) {
      throw new Error('fleet registry agent identifiers must be canonical lowercase slugs');
    }
    const entry = mapping(rawEntry);
    if (!entry) {
      throw new Error(`fleet registry metadata for ${agentId} must be a mapping`);
    }
    if (nonblank(entry.repo) === repo) repositoryRecords.push({ agentId, entry });
  }

  if (!repositoryRecords.length) {
    throw new Error(`repository ${repo} has no registry route`);
  }

  const eligible = repositoryRecords.filter(({ agentId, entry }) => {
    const bloodbank = mapping(entry.bloodbank);
    return Boolean(
      nonblank(entry.profile_name) &&
      bloodbank &&
      bloodbankActivation(bloodbank) === 'enabled' &&
      bloodbank.gateway_scope === 'fleet' &&
      bloodbank.target_agent_id === agentId
    );
  });

  if (!eligible.length) {
    throw new Error(`registry route for repository ${repo} is not eligible for fleet commands`);
  }
  if (eligible.length !== 1) {
    throw new Error(`repository ${repo} has ambiguous eligible fleet routes`);
  }
  return eligible[0].agentId;
}
