import { deterministicUuid } from './nats';

/** Where a ticket's owning agent came from, and whether the bus may address it.
 *
 * `eligible: false` is a first-class answer, not an error. One shared trigger
 * sees every project's tickets, so an ineligible route has to be reportable
 * without turning 27 switched-off projects into 27 failing executions.
 */
export interface FleetRoute {
  agentId: string;
  profileName?: string;
  eligible: boolean;
  why: string;
  projectPath?: string;
  workspace?: string;
  identifier?: string;
  matchedBy: 'board' | 'fallback' | 'none';
}

/** The ticket facts a fleet invocation needs, lifted out of a bus envelope. */
export interface TicketFacts {
  repo: string;
  boardId: string;
  ticketKey: string;
  ticketId: string;
  title: string;
  workspace: string;
  phase: string;
  previousPhase: string;
  providerEventType: string;
  url: string;
}

function mapping(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function text(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

/** Resolve the agent that owns a board, board id first and repo slug second.
 *
 * Board id is the primary key on purpose: it is the one identifier a provider
 * webhook always carries, and it is what tells a multi-tenant Plane which
 * workspace to answer as. The `<repo>-pm` fallback keeps a project that has not
 * been reconciled into the registry's `plane` block addressable by convention.
 */
export function resolveFleetAgentForBoard(
  registryValue: unknown,
  boardIdValue: string,
  repoValue: string,
): FleetRoute {
  const boardId = text(boardIdValue);
  const repo = text(repoValue);
  const fallback = repo ? `${repo}-pm` : '';

  const root = mapping(registryValue);
  const agents = root && mapping(root.agents);
  if (!agents) {
    return {
      agentId: fallback,
      eligible: false,
      why: 'fleet registry has no agents mapping',
      matchedBy: 'none',
    };
  }

  let agentId = '';
  let entry: Record<string, unknown> | undefined;
  let matchedBy: FleetRoute['matchedBy'] = 'none';

  if (boardId) {
    for (const [candidateId, rawEntry] of Object.entries(agents)) {
      const candidate = mapping(rawEntry);
      const plane = candidate && mapping(candidate.plane);
      if (!plane) continue;
      const ids = [text(plane.project_id), text(plane.board_id)].filter(Boolean);
      if (ids.includes(boardId)) {
        agentId = candidateId;
        entry = candidate;
        matchedBy = 'board';
        break;
      }
    }
  }

  if (!entry) {
    const candidate = fallback ? mapping(agents[fallback]) : undefined;
    if (!candidate) {
      return {
        agentId: fallback,
        eligible: false,
        why: boardId
          ? `no registry entry matches board ${boardId} and no fallback agent ${fallback || '(none)'} exists`
          : `no board id was given and no fallback agent ${fallback || '(none)'} exists`,
        matchedBy: 'none',
      };
    }
    agentId = fallback;
    entry = candidate;
    matchedBy = 'fallback';
  }

  const plane = mapping(entry.plane) || {};
  const base = {
    agentId,
    profileName: text(entry.profile_name) || undefined,
    projectPath: text(entry.project_path) || text(entry.repo_path) || undefined,
    workspace: text(plane.workspace) || undefined,
    identifier: text(plane.identifier) || undefined,
    matchedBy,
  };

  // Eligibility mirrors hermes-gateway `contract.py`: it builds a fleet route
  // only when all four of these agree, and refuses the command otherwise. We
  // check the same four here so a switched-off project is a readable skip
  // rather than a RouteInvalid buried in gateway logs.
  if (!base.profileName) {
    return { ...base, eligible: false, why: `${agentId} has no profile_name` };
  }
  const bloodbank = mapping(entry.bloodbank);
  if (!bloodbank) {
    return { ...base, eligible: false, why: `${agentId} has no bloodbank block` };
  }
  if (bloodbank.enabled !== true) {
    return {
      ...base,
      eligible: false,
      why: `${agentId} is registry-defined but bloodbank.enabled is not true`,
    };
  }
  if (bloodbank.gateway_scope !== 'fleet') {
    return { ...base, eligible: false, why: `${agentId} gateway_scope is not 'fleet'` };
  }
  if (text(bloodbank.target_agent_id) !== agentId) {
    return {
      ...base,
      eligible: false,
      why: `${agentId} target_agent_id mismatch (${text(bloodbank.target_agent_id) || 'unset'})`,
    };
  }
  return { ...base, eligible: true, why: 'eligible' };
}

/** One correlation id per ticket, stable across replays of the same webhook.
 *
 * The generic fallback hashes the event type, which is a constant: every
 * invocation would share one correlation id, and therefore one thread and one
 * idempotency key, collapsing distinct tickets into a single conversation and
 * colliding on dedup. Deriving from the ticket gives each its own thread while
 * keeping a redelivered webhook idempotent.
 */
export function ticketCorrelationId(boardId: string, ticketRef: string): string {
  return deterministicUuid(`plane:${text(boardId)}:${text(ticketRef)}`);
}

/** Lift ticket facts from a full CloudEvent envelope or a bare data object. */
export function ticketFactsFromEnvelope(value: unknown): TicketFacts {
  const root = mapping(value) || {};
  const data = mapping(root.data) || root;
  const ticket = mapping(data.ticket) || {};
  return {
    repo: text(data.repo),
    boardId: text(data.board_id) || text(data.project_id),
    ticketKey: text(data.ticket_key),
    ticketId: text(data.ticket_id) || text(data.task_id) || text(ticket.id),
    title: text(data.title) || text(ticket.name),
    workspace: text(data.workspace),
    phase: text(data.phase),
    previousPhase: text(data.previous_phase),
    providerEventType: text(data.provider_event_type),
    url: text(data.url),
  };
}

/** The two facts an agent cannot derive and pays a failed tool call to guess.
 *
 * The checkout path comes from the registry. The board id is what tells a
 * multi-workspace Plane which tenant to answer as: a call addressed only by
 * ticket key resolves against the gateway's default workspace, which is the
 * wrong one for every board but 33GOD's, and comes back 403.
 */
function whereThingsAre(facts: TicketFacts, projectPath?: string): string {
  const lines: string[] = [];
  if (projectPath) lines.push(`Repo checkout: ${projectPath}`);
  if (facts.boardId) lines.push(`Plane board id: ${facts.boardId}`);
  if (!lines.length) return '';
  return (
    '\nWhere things are — use these verbatim, do not guess or search for them:\n' +
    lines.join('\n') +
    '\nPass that board id as `project_id` on every Plane tool call. The board ' +
    'lives in a workspace that is not the default one, and a call addressed ' +
    'only by ticket key is answered by the wrong workspace (HTTP 403).\n'
  );
}

function provenance(facts: TicketFacts): string {
  return `\nProvider provenance: workspace ${facts.workspace || '(unknown)'}, board ${
    facts.boardId || '(unknown)'
  }.`;
}

function ticketRef(facts: TicketFacts): string {
  const key = facts.ticketKey || facts.ticketId || '(unknown)';
  return facts.title ? `${key} — ${JSON.stringify(facts.title)}` : key;
}

/** Grooming: enrich one existing ticket in place, and stamp that it happened. */
export function groomingPrompt(
  facts: TicketFacts,
  projectPath?: string,
  doneLabel = 'lifecycle:triaged',
): string {
  const stamp = doneLabel
    ? `\nWhen the pass is complete, add the label \`${doneLabel}\` to the ticket. ` +
      'That label is the pipeline\'s completion marker: downstream automation ' +
      'treats a ticket in Todo carrying it as ready to be delegated, so add it ' +
      'last and only if you actually finished the pass. If you had to stop and ' +
      'ask a question instead, do not add it.\n'
    : '';
  return (
    `Groom the ticket that was just created on the ${facts.repo || 'project'} board: ` +
    `${ticketRef(facts)}.\n` +
    `${whereThingsAre(facts, projectPath)}\n` +
    'This is a GROOMING pass on one existing ticket. Improve that ticket in ' +
    'place. Do NOT create, split, or decompose it into new tickets, and do not ' +
    'change its state or work the task.\n\n' +
    "Read the repo's `.project.json` and the board's own conventions, then look " +
    'at what is already in flight so this ticket agrees with its neighbours ' +
    'rather than inventing a parallel vocabulary. Apply the 33GOD standards you ' +
    'find there:\n' +
    '  - labels, using the label names the board already uses\n' +
    '  - the exposure label, whose two names come from `.project.json` ' +
    '`activity_report.board.exposure_labels` (external -> the client\'s board, ' +
    'internal -> the client-admin board). Apply one ONLY when the ticket ' +
    'genuinely belongs in front of that audience; most tickets get neither, and ' +
    'neither is the default.\n' +
    "  - cycle assignment per the board's cycle rules\n" +
    '  - module attachment\n' +
    '  - priority\n' +
    "  - start date where the board's rules imply one\n\n" +
    'If the description is one thin sentence, expand it into something a person ' +
    'who just walked in could act on — but keep the author\'s meaning and do not ' +
    'invent scope or commitments. If you genuinely cannot tell what the ticket ' +
    'means, comment asking for the detail you need instead of guessing.\n' +
    stamp +
    provenance(facts)
  );
}

/** Delegation: a groomed ticket a human promoted to Todo becomes real work. */
export function delegationPrompt(
  facts: TicketFacts,
  projectPath?: string,
  requiredLabel = 'lifecycle:triaged',
): string {
  const gate = requiredLabel
    ? `First check the precondition: the ticket must carry the label ` +
      `\`${requiredLabel}\`, which is how the grooming pass records that it ` +
      'finished. If the label is absent, do the grooming pass yourself — labels, ' +
      'module, priority, cycle, and a description someone who just walked in ' +
      'could act on — add the label, and then continue here. Do not delegate a ' +
      'ticket nobody has groomed.\n\n'
    : '';
  return (
    `${ticketRef(facts)} has moved into Todo on the ${facts.repo || 'project'} ` +
    'board. Pick it up and get the work moving.\n' +
    `${whereThingsAre(facts, projectPath)}\n` +
    gate +
    'Then decide what happens to it, in this order:\n' +
    '  1. Judge readiness. Acceptance criteria have to be enumerated and ' +
    'testable — a reader must be able to say pass or fail on each one without ' +
    'asking you. If they are not, fix them before anything else.\n' +
    '  2. Judge fit. If this ticket is genuinely two or more pieces of work, say ' +
    'so in a comment and split it. Grooming is forbidden from splitting; you are ' +
    'not.\n' +
    '  3. Delegate the work to a worker agent. You are the PM: you hold the ' +
    'roadmap and you do not write the code yourself. Give the worker the ticket ' +
    'key, the checkout path, the board id, and the acceptance criteria verbatim.\n' +
    '  4. Move the ticket to In Progress and set its start date to now. The ' +
    'start date is when work actually begins, not when the ticket was filed, and ' +
    'that transition is what tells the rest of the board this is claimed.\n' +
    '  5. Leave the assignee empty. Work is claimed by state, not by assignment ' +
    '— the pool model depends on it.\n\n' +
    'If you cannot delegate it — blocked on a decision, on another ticket, or on ' +
    'something only a person can answer — do not move it to In Progress. Say ' +
    'what it is waiting on in a comment and move it to the state the board uses ' +
    'for that, so it stops looking like available work.\n' +
    provenance(facts)
  );
}
