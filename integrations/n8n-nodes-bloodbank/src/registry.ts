const CANONICAL_AGENT_ID = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

function mapping(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function nonblank(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
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
      bloodbank.enabled === true &&
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
