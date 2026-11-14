import { deterministicUuid } from './nats';
import { bloodbankActivation, describeActivation } from './registry';
import { labelNames, orderOwners, pickOwner, scopeFields } from './scope';
import type { Scoped } from './scope';

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
  /** Why an ineligible route is ineligible, in a form a filter can switch on.
   *  `no_route`: no agent owns the board. `invalid_policy`: the row's
   *  `bloodbank.enabled` is present but not a boolean. `ineligible`: every
   *  other policy refusal. Absent when eligible. */
  code?: 'no_route' | 'invalid_policy' | 'ineligible';
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
  /** The ticket's label names, lowercased: what picks the owner on a shared board. */
  labels: string[];
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
 * Several agents may share one board. Each then declares the label(s) it takes
 * (`plane.scope_label`, a string or a list) and at most one declares
 * `plane.scope_default: true`. A ticket goes to the agent whose scope label it
 * carries, else to the default agent; with no default declared the choice is
 * still deterministic (agent id order), never registry file order.
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
  ticketLabels: unknown = [],
): FleetRoute {
  const labels = labelNames(ticketLabels);
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
      code: 'no_route',
      matchedBy: 'none',
    };
  }

  let agentId = '';
  let entry: Record<string, unknown> | undefined;
  let matchedBy: FleetRoute['matchedBy'] = 'none';

  if (boardId) {
    const owners: Array<Scoped & { id: string; entry: Record<string, unknown> }> = [];
    for (const [candidateId, rawEntry] of Object.entries(agents)) {
      const candidate = mapping(rawEntry);
      const plane = candidate && mapping(candidate.plane);
      if (!candidate || !plane) continue;
      const ids = [text(plane.project_id), text(plane.board_id)].filter(Boolean);
      if (ids.includes(boardId)) owners.push({ id: candidateId, entry: candidate, ...scopeFields(plane) });
    }
    if (owners.length) {
      const owner = pickOwner(orderOwners(owners, (candidate) => candidate.id), labels);
      agentId = owner.id;
      entry = owner.entry;
      matchedBy = 'board';
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
        code: 'no_route',
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
  //
  // `bloodbank.enabled` follows the fleet rule that no key means enabled: an
  // ABSENT key activates the row, explicit `false` switches it off, and a
  // present non-boolean is invalid (and therefore off) rather than guessed at.
  if (!base.profileName) {
    return { ...base, eligible: false, code: 'ineligible', why: `${agentId} has no profile_name` };
  }
  const bloodbank = mapping(entry.bloodbank);
  if (!bloodbank) {
    return { ...base, eligible: false, code: 'ineligible', why: `${agentId} has no bloodbank block` };
  }
  const activation = bloodbankActivation(bloodbank);
  if (activation === 'disabled') {
    return {
      ...base,
      eligible: false,
      code: 'ineligible',
      why: `${agentId} is registry-defined but bloodbank.enabled is false`,
    };
  }
  if (activation === 'invalid') {
    return {
      ...base,
      eligible: false,
      code: 'invalid_policy',
      why: `${agentId} has an invalid bloodbank.enabled (${describeActivation(bloodbank)}); it must be a YAML boolean or absent`,
    };
  }
  if (bloodbank.gateway_scope !== 'fleet') {
    return { ...base, eligible: false, code: 'ineligible', why: `${agentId} gateway_scope is not 'fleet'` };
  }
  if (text(bloodbank.target_agent_id) !== agentId) {
    return {
      ...base,
      eligible: false,
      code: 'ineligible',
      why: `${agentId} target_agent_id mismatch (${text(bloodbank.target_agent_id) || 'unset'})`,
    };
  }
  return { ...base, eligible: true, why: 'eligible' };
}

/** One correlation id per ticket: the ticket is the conversation.
 *
 * The generic fallback hashes the event type, which is a constant: every
 * invocation would share one correlation id, and therefore one thread,
 * collapsing distinct tickets into a single conversation. Deriving from the
 * ticket gives each its own thread.
 *
 * Pass the provider ticket id (UUID) when there is one: that is exactly what
 * the Plane webhook ingress derives (`plane:<board_id>:<ticket_id>`), so a
 * command built here lands on the same correlation id as the fact that caused
 * it. The ticket key is only a fallback for callers that have nothing else.
 *
 * Correlation is NOT idempotency. Deduplication keys on the command id, which
 * the fleet node derives from the triggering event id (`fleetCommandId`).
 */
export function ticketCorrelationId(boardId: string, ticketRef: string): string {
  return deterministicUuid(`plane:${text(boardId)}:${text(ticketRef)}`);
}

/** A command id that is a pure function of the event that caused it.
 *
 * A redelivered trigger event (same event id — the Plane ingress derives event
 * ids deterministically from the webhook, so a Plane retry qualifies) yields the
 * same command id and therefore the same `idempotency_key`
 * (`agent.invocation.start:target:<agent>:command:<command_id>`). The gateway
 * journals on command_id plus a digest of the whole envelope, so the Fleet node
 * also stamps the causing event's time (`observedAt`): together they make a
 * replayed dispatch byte-identical, and the gateway treats it as the command it
 * already has rather than a colliding one. Returns undefined when there is no
 * causing event id, so the publisher mints a fresh one.
 */
export function fleetCommandId(
  causationId: string | undefined,
  operation: string,
  agentId: string,
): string | undefined {
  const cause = text(causationId);
  if (!cause) return undefined;
  return deterministicUuid(`agent.invocation.start:${text(operation)}:${text(agentId)}:${cause}`);
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
    labels: labelNames(ticket.labels ?? data.labels),
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

/** Both lanes: how an agent may touch labels without clobbering the pipeline's. */
const LABEL_RULES =
  '\nLabels: change them one at a time with the Plane tool\'s add/remove label ' +
  'actions; never rewrite the whole label list. `agent:working` belongs to the ' +
  'pipeline: it shows a turn is running on this ticket and is cleared ' +
  'automatically when your turn ends. Never add or remove it yourself, and never ' +
  'touch another lifecycle label except the ones this prompt names.\n';

/** Triage (Krebs lifecycle v2): refine one Todo ticket in place and stamp it.
 *
 * The lane that sends this prompt is deterministic: it has already refused a
 * ticket that carries `lifecycle:triaged` or a hold label, so this prompt never
 * has to police those. It must still never change state or start the work.
 * The function keeps its pre-v2 name (`groomingPrompt`) because callers import it.
 */
export function groomingPrompt(
  facts: TicketFacts,
  projectPath?: string,
  doneLabel = 'lifecycle:triaged',
): string {
  const stamp = doneLabel
    ? `\nEnd the turn by adding the label \`${doneLabel}\` to the ticket, as the ` +
      'LAST thing you do. That label is the pipeline\'s completion marker: ' +
      'Krebs only dispatches a Todo ticket that carries it, so add it only if ' +
      'you actually finished the triage, and do nothing after it.\n\n' +
      'If you cannot finish because you need an answer from a person, do not add ' +
      `\`${doneLabel}\`. Instead add the label \`lifecycle:needs-input\` and post ` +
      'the question as a comment on the ticket, written so the person can answer ' +
      'it in one reply. Then stop.\n'
    : '';
  return (
    `Triage the ticket that just entered Todo on the ${facts.repo || 'project'} board: ` +
    `${ticketRef(facts)}.\n` +
    `${whereThingsAre(facts, projectPath)}\n` +
    'This is a TRIAGE turn on one existing ticket. Refine that ticket in place: ' +
    'acceptance criteria, scope and labels. Never change the ticket\'s state ' +
    '(it stays in Todo; Krebs, not you, moves it to In Progress). Do NOT create, ' +
    'split or decompose it into new tickets, and do not start or work the task.\n\n' +
    "Read the repo's `.project.json` and the board's own conventions, then look " +
    'at what is already in flight so this ticket agrees with its neighbours ' +
    'rather than inventing a parallel vocabulary. Refine, using the standards ' +
    'you find there:\n' +
    '  - acceptance criteria: enumerated and testable, so a reader can say pass ' +
    'or fail on each one without asking you\n' +
    '  - scope: what is in, and what is explicitly out\n' +
    '  - labels, using the label names the board already uses\n' +
    '  - the exposure label, whose two names come from `.project.json` ' +
    '`activity_report.board.exposure_labels` (external -> the client\'s board, ' +
    'internal -> the client-admin board). Apply one ONLY when the ticket ' +
    'genuinely belongs in front of that audience; most tickets get neither, and ' +
    'neither is the default.\n' +
    "  - cycle assignment, module attachment, priority and start date where the " +
    "board's rules imply them\n\n" +
    'If the description is one thin sentence, expand it into something a person ' +
    'who just walked in could act on, but keep the author\'s meaning and do not ' +
    'invent scope or commitments. If a ticket really is two pieces of work, say ' +
    'so in a comment; the PM splits it, not you.\n' +
    stamp +
    LABEL_RULES +
    provenance(facts)
  );
}

/** Delegation (Krebs lifecycle v2): implement a ticket Krebs already started.
 *
 * `krebs tick` is the only dispatcher: it has moved the ticket to In Progress
 * and set `agent:working` before this prompt is sent. The n8n delegation lane
 * only refuses safely (`not_triaged`, `hitl_blocked`) if something still fires it.
 * `requiredLabel` is kept for signature compatibility with older callers.
 */
export function delegationPrompt(
  facts: TicketFacts,
  projectPath?: string,
  requiredLabel = 'lifecycle:triaged',
): string {
  const recheck =
    'Before anything else, re-read the ticket live from Plane: its state, ' +
    'labels and latest comments. This request may be older than the ticket. If ' +
    'it is no longer In Progress, if it carries a hold label ' +
    '(`lifecycle:needs-input`, `lifecycle:blocked` or `agent:failed`), or if it ' +
    'already carries `lifecycle:review`, stop: say so in your reply and change ' +
    'nothing. The `agent:working` label alone is not a claim; the pipeline puts ' +
    'it on for this very turn.\n\n';
  const gate = requiredLabel
    ? `The ticket should carry \`${requiredLabel}\` (triage finished). If it ` +
      'does not, say so in a comment and stop; do not triage it yourself.\n\n'
    : '';
  return (
    `${ticketRef(facts)} is on the ${facts.repo || 'project'} board and Krebs has ` +
    'just dispatched it to you: it is ALREADY In Progress. Do not move it, and do ' +
    'not change its state to start work. Implement it.\n' +
    `${whereThingsAre(facts, projectPath)}\n` +
    recheck +
    gate +
    'Then work it:\n' +
    '  1. Read the acceptance criteria. They have to be enumerated and testable. ' +
    'If one is ambiguous, ask in a comment (see waiting, below) rather than guess.\n' +
    '  2. Do the work, to the criteria, in the repo checkout above. Leave the ' +
    'assignee empty; work is claimed by state, not by assignment.\n' +
    '  3. Hand back with evidence. Post a comment that proves each acceptance ' +
    'criterion (commands run and their output, commit SHAs, links), then:\n' +
    '     - if this scope requires review (the ticket needs a verdict from a ' +
    'Launch Director or the PM), add the label `lifecycle:review`. The ticket ' +
    'stays In Progress; the verdict giver moves it from there.\n' +
    '     - otherwise close the ticket Done, with the evidence comment.\n\n' +
    'Waiting: if you cannot proceed because you need an answer from a person, ' +
    'add the label `lifecycle:needs-input` and post the question as a comment. ' +
    'If you are waiting on another ticket or an outside dependency, add ' +
    '`lifecycle:blocked` and say in a comment what it waits on (record a ' +
    'blocking ticket as a Plane relation). Never change lanes to signal waiting: ' +
    'the ticket stays In Progress and the label frees the slot.\n' +
    LABEL_RULES +
    provenance(facts)
  );
}
