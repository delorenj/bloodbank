import { providerAliases } from './nodes/Bloodbank/eventSchemas';
import { optionName } from './options';
import type { ProjectBoard } from './projects';

export interface PlaneProjectRoute {
  boardId: string;
  repo: string;
  slug: string;
  workspace: string;
  boardKey?: string;
  /** Which enrollment record supplied this route. */
  source?: 'pjangler' | 'hermes' | 'manifest';
}

export interface PlaneEventBinding {
  name: string;
  value: string;
  canonicalType: string;
  description: string;
}

/** Every provider_event_type this normalizer can emit.
 *
 * Each one MUST be declared as an `x-provider-aliases` entry on its canonical
 * schema — the schema tree is the single source for provider aliases, and the
 * normalizer looks its canonical type up there rather than hardcoding it. A
 * test fails the build when the two drift.
 */
export const PLANE_PROVIDER_EVENT_TYPES = {
  boardCreated: 'plane.board.created',
  ticketCreated: 'plane.ticket.created',
  ticketUpdated: 'plane.ticket.updated',
  ticketTransitioned: 'plane.ticket.transitioned',
  ticketDeleted: 'plane.ticket.deleted',
  ticketCommented: 'plane.ticket.commented',
} as const;

/** The Plane trigger aliases, derived from the schema-declared provider aliases. */
export const planeEventBindings: PlaneEventBinding[] = providerAliases
  .filter((alias) => alias.provider === 'plane')
  .map((alias) => ({
    name: optionName('Plane', alias.label, alias.value),
    value: alias.value,
    canonicalType: alias.canonicalType,
    description: alias.description,
  }));

export function canonicalTypeForProviderEvent(providerEventType: string): string {
  const alias = providerAliases.find((candidate) => candidate.value === providerEventType);
  if (!alias) {
    throw new Error(
      `provider event ${providerEventType} is not declared in any schema's x-provider-aliases`,
    );
  }
  return alias.canonicalType;
}

export interface NormalizedPlaneEvent {
  canonicalType: string;
  providerEventType: string;
  data: Record<string, unknown>;
  extensions: Record<string, string>;
  orderingKey: string;
  dedupeKey: string;
  /** The dedupe key names the fact itself, not just one observation of it: a
   *  ticket or board creation, a deletion, a comment by its id. Only such a key
   *  is sent as `Nats-Msg-Id`, because JetStream drops a second message with
   *  the same id outright. An update's key is as distinct as Plane lets it be
   *  (see `updateDedupeKey`), which is enough for an event id, but it is still
   *  inferred from the delivery, so updates carry no `Nats-Msg-Id`. */
  stableId: boolean;
  observedAt: string;
}

/** What became of one Plane delivery.
 *
 * - `routed`: a canonical fact to publish.
 * - `unrouted`: a supported event on a board no enrolled project claims. This
 *   is a gap in enrollment, not a no-op, and callers must surface it.
 * - `unsupported`: an event type Bloodbank does not model (project updates,
 *   cycles, modules) or a payload too malformed to read. A legitimate no-op.
 */
export type PlaneClassification =
  | { status: 'routed'; event: NormalizedPlaneEvent; route?: PlaneProjectRoute }
  | {
      status: 'unrouted';
      reason: string;
      boardId: string;
      providerEvent: string;
      action: string;
      workspace?: string;
    }
  | { status: 'unsupported'; reason: string; providerEvent?: string; action?: string; boardId?: string };

/** Build the Plane board-id routing table from the shared Hermes registry. */
export function planeRoutesFromRegistry(registryValue: unknown): Map<string, PlaneProjectRoute> {
  const root = record(registryValue);
  const agents = record(root.agents ?? root);
  const routes = new Map<string, PlaneProjectRoute>();
  for (const agent of Object.values(agents)) {
    const entry = record(agent);
    const plane = record(entry.plane);
    const boardId = firstText(plane.project_id, plane.board_id);
    const repo = firstText(entry.repo);
    if (!boardId || !repo || routes.has(boardId)) continue;
    routes.set(boardId, {
      boardId,
      repo,
      slug: firstText(entry.slug) ?? repo,
      workspace: firstText(plane.workspace) ?? 'unknown',
      boardKey: firstText(plane.identifier),
      source: 'hermes',
    });
  }
  return routes;
}

/** Project paths of Hermes rows that name no Plane board of their own. */
export function unboundRegistryProjectPaths(registryValue: unknown): string[] {
  const root = record(registryValue);
  const agents = record(root.agents ?? root);
  const paths: string[] = [];
  for (const agent of Object.values(agents)) {
    const entry = record(agent);
    const plane = record(entry.plane);
    if (firstText(plane.project_id, plane.board_id)) continue;
    const path = firstText(entry.project_path, entry.repo_path);
    if (path && !paths.includes(path)) paths.push(path);
  }
  return paths;
}

/** One routing table from every enrollment record.
 *
 * pjangler (the index of every repo's `.project.json`) is canonical project
 * identity, so it wins: its slug becomes `data.repo`. Hermes rows fill in
 * boards pjangler does not index, and fill workspace/identifier gaps. A
 * manifest read directly (a Hermes row with a project path but no board) is the
 * last resort for repos neither index knows by board.
 */
export function mergePlaneRoutes(
  hermes: ReadonlyMap<string, PlaneProjectRoute>,
  projects: ProjectBoard[] = [],
  manifests: ProjectBoard[] = [],
): Map<string, PlaneProjectRoute> {
  const routes = new Map<string, PlaneProjectRoute>();
  for (const board of manifests) {
    if (routes.has(board.boardId)) continue;
    routes.set(board.boardId, routeFromBoard(board));
  }
  for (const [boardId, route] of hermes) routes.set(boardId, { ...route });
  for (const board of projects) {
    const known = routes.get(board.boardId);
    routes.set(board.boardId, {
      boardId: board.boardId,
      repo: board.repo,
      slug: board.repo,
      workspace: board.workspace || usableWorkspace(known?.workspace) || 'unknown',
      boardKey: board.identifier || known?.boardKey,
      source: 'pjangler',
    });
  }
  return routes;
}

function routeFromBoard(board: ProjectBoard): PlaneProjectRoute {
  return {
    boardId: board.boardId,
    repo: board.repo,
    slug: board.repo,
    workspace: board.workspace || 'unknown',
    boardKey: board.identifier,
    source: board.source,
  };
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return undefined;
}

function firstText(...values: unknown[]): string | undefined {
  for (const value of values) {
    const candidate = text(value);
    if (candidate) return candidate;
  }
  return undefined;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function usableWorkspace(value: unknown): string | undefined {
  const candidate = text(value);
  if (!candidate || candidate === 'unknown' || UUID.test(candidate)) return undefined;
  return candidate;
}

function entityId(value: unknown): string | undefined {
  return typeof value === 'object' && value !== null
    ? firstText((value as Record<string, unknown>).id)
    : firstText(value);
}

function normalizeTimestamp(value: unknown, fallback: string): string {
  const candidate = text(value);
  if (!candidate) return fallback;
  const parsed = new Date(candidate);
  return Number.isNaN(parsed.valueOf()) ? fallback : parsed.toISOString();
}

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'unknown-project';
}

function stateValue(value: unknown): string | null {
  if (typeof value === 'object' && value !== null) {
    const state = value as Record<string, unknown>;
    return firstText(state.name, state.slug, state.group, state.state_type, state.id) ?? null;
  }
  return firstText(value) ?? null;
}

function tpBand(value: unknown): string | null {
  const source = typeof value === 'object' && value !== null
    ? firstText(
        (value as Record<string, unknown>).group,
        (value as Record<string, unknown>).state_type,
      )
    : firstText(value);
  if (!source) return null;
  const normalized = source.toLowerCase().replace(/[- ]+/g, '_');
  const aliases: Record<string, string> = {
    backlog: 'backlog',
    unstarted: 'unstarted',
    started: 'started',
    in_progress: 'started',
    in_review: 'in_review',
    review: 'in_review',
    completed: 'completed',
    done: 'completed',
    canceled: 'completed',
    cancelled: 'completed',
  };
  return aliases[normalized] ?? null;
}

function changedFields(payload: Record<string, unknown>, data: Record<string, unknown>): string[] {
  const activity = record(payload.activity ?? data.activity);
  const fields = new Set<string>();
  const field = firstText(activity.field, activity.field_name);
  if (field) fields.add(field);
  const declared = payload.changed_fields ?? data.changed_fields;
  if (Array.isArray(declared)) {
    for (const value of declared) {
      const candidate = text(value);
      if (candidate) fields.add(candidate);
    }
  } else {
    const candidate = text(declared);
    if (candidate) fields.add(candidate);
  }
  return [...fields].sort();
}

function ticketKey(route: PlaneProjectRoute, data: Record<string, unknown>): string | null {
  const explicit = firstText(data.identifier, data.ticket_key);
  if (explicit) return explicit;
  const sequence = firstText(data.sequence_id);
  if (!sequence || !route.boardKey) return null;
  return `${route.boardKey}-${sequence}`;
}

/** The workspace SLUG for this delivery — never its UUID.
 *
 * Plane puts the slug at the top level (`workspace_slug`) and the UUID in
 * `data.workspace`. A registry-declared slug wins; the payload fills the gap.
 */
function workspaceSlug(
  payload: Record<string, unknown>,
  data: Record<string, unknown>,
  route?: PlaneProjectRoute,
): string {
  const entity = (value: unknown): string | undefined => {
    if (typeof value !== 'object' || value === null) return usableWorkspace(value);
    const workspace = value as Record<string, unknown>;
    return usableWorkspace(workspace.slug) ?? usableWorkspace(workspace.name);
  };
  return (
    usableWorkspace(route?.workspace) ??
    usableWorkspace(payload.workspace_slug) ??
    entity(payload.workspace_detail) ??
    entity(data.workspace_detail) ??
    entity(data.workspace) ??
    entity(payload.workspace) ??
    'unknown'
  );
}

function normalizeAction(value: unknown): string {
  const action = (firstText(value) ?? 'updated').toLowerCase();
  const aliases: Record<string, string> = {
    create: 'created',
    created: 'created',
    update: 'updated',
    updated: 'updated',
    delete: 'deleted',
    deleted: 'deleted',
  };
  return aliases[action] ?? action;
}

function supported(event: string, action: string): boolean {
  if (event === 'project') return action === 'created';
  if (event === 'issue') return ['created', 'updated', 'deleted'].includes(action);
  if (event === 'issue_comment') return ['created', 'commented'].includes(action);
  return false;
}

/** Classify one Plane webhook and, when it is routable, normalize it into
 *  exactly one provider-neutral Bloodbank fact.
 *
 * Plane names remain available as data.provider_event_type and as n8n trigger
 * aliases. They intentionally do not enter CloudEvents type/subject tokens.
 */
export function classifyPlaneWebhook(
  payloadValue: unknown,
  routes: ReadonlyMap<string, PlaneProjectRoute>,
  receivedAt = new Date().toISOString(),
): PlaneClassification {
  const payload = record(payloadValue);
  const event = firstText(payload.event)?.toLowerCase();
  const action = normalizeAction(payload.action);
  const data = record(payload.data);
  if (!event || !Object.keys(data).length) {
    return { status: 'unsupported', reason: 'payload has no event or no data', providerEvent: event, action };
  }
  if (!supported(event, action)) {
    return { status: 'unsupported', reason: `Plane ${event}.${action} is not modelled on the bus`, providerEvent: event, action };
  }

  const rawProject = data.project ?? data.project_id ?? payload.project;
  const boardId = event === 'project'
    ? firstText(data.id, data.project_id)
    : entityId(rawProject);
  if (!boardId) {
    return { status: 'unsupported', reason: `Plane ${event}.${action} carries no board id`, providerEvent: event, action };
  }

  const route = routes.get(boardId);
  // A creation is observed when the ticket was created, not when it was last
  // touched: the reconcile sweep reads a ticket hours later, and its fact must
  // carry the same time as the webhook's would have.
  const observedAt = normalizeTimestamp(
    event === 'issue' && action === 'created'
      ? data.created_at ?? data.updated_at ?? payload.timestamp ?? payload.created_at
      : data.updated_at ?? data.created_at ?? payload.timestamp ?? payload.created_at,
    receivedAt,
  );

  if (event === 'project') {
    // A board nobody has claimed yet is still a fact worth publishing: it is
    // what a project provisioner waits for. It carries repo=null rather than a
    // slug guessed from the board name, which would name a repo that may never
    // exist.
    const workspace = workspaceSlug(payload, data, route);
    const boardKey = route?.boardKey ?? firstText(data.identifier);
    const slug = route?.slug ?? slugify(firstText(data.slug, data.identifier, data.name) ?? boardId);
    const providerEventType = PLANE_PROVIDER_EVENT_TYPES.boardCreated;
    return {
      status: 'routed',
      route,
      event: {
        canonicalType: canonicalTypeForProviderEvent(providerEventType),
        providerEventType,
        observedAt,
        orderingKey: `board:${boardId}`,
        dedupeKey: `${providerEventType}:${boardId}:${observedAt}`,
        stableId: true,
        extensions: { workspace, board_id: boardId, slug, provider_event_type: providerEventType },
        data: {
          repo: route?.repo ?? null,
          slug,
          workspace,
          board_id: boardId,
          project_id: boardId,
          provider: 'plane',
          board_key: boardKey ?? null,
          provider_event_type: providerEventType,
          timestamp: observedAt,
          board: data,
        },
      },
    };
  }

  if (!route) {
    return {
      status: 'unrouted',
      reason: `no enrolled project claims Plane board ${boardId}`,
      boardId,
      providerEvent: event,
      action,
      workspace: workspaceSlug(payload, data),
    };
  }

  const workspace = workspaceSlug(payload, data, route);
  const base = {
    repo: route.repo,
    slug: route.slug,
    workspace,
    board_id: route.boardId,
    project_id: route.boardId,
    provider: 'plane',
  };
  const extensions = { workspace, board_id: route.boardId, slug: route.slug };

  if (event === 'issue') {
    const ticketId = firstText(data.id);
    if (!ticketId) {
      return { status: 'unsupported', reason: 'Plane issue payload carries no issue id', providerEvent: event, action, boardId };
    }
    const fields = changedFields(payload, data);
    const activity = record(payload.activity ?? data.activity);
    const isTransition = action === 'updated' && fields.some((field) => field === 'state' || field === 'state_id');
    const providerEventType = action === 'created'
      ? PLANE_PROVIDER_EVENT_TYPES.ticketCreated
      : action === 'deleted'
        ? PLANE_PROVIDER_EVENT_TYPES.ticketDeleted
        : isTransition
          ? PLANE_PROVIDER_EVENT_TYPES.ticketTransitioned
          : PLANE_PROVIDER_EVENT_TYPES.ticketUpdated;
    const currentState = data.state_detail ?? data.state;
    const previousState = activity.old_value ?? activity.previous_value;
    const normalizedFields = action === 'deleted' && !fields.length ? ['deleted'] : fields;
    const key = ticketKey(route, data);
    const triggerSource = firstText(payload.trigger_source, data.trigger_source) ?? 'plane-webhook';
    const common = {
      ...base,
      task_id: ticketId,
      ticket_id: ticketId,
      ticket_key: key,
      title: firstText(data.name, data.title) ?? key ?? ticketId,
      provider_event_type: providerEventType,
      phase: action === 'deleted' ? 'deleted' : stateValue(currentState),
      tp_band: action === 'deleted' ? 'completed' : tpBand(currentState),
      timestamp: observedAt,
      ticket: data,
    };
    return {
      status: 'routed',
      route,
      event: {
        canonicalType: canonicalTypeForProviderEvent(providerEventType),
        providerEventType,
        observedAt,
        orderingKey: `task:${route.repo}:${ticketId}`,
        // A ticket is created once, so its creation fact is keyed on the ticket
        // alone: the webhook and the reconcile sweep derive the same event id
        // (and Nats-Msg-Id) and a race between them collapses into one fact.
        dedupeKey: action === 'created'
          ? createdDedupeKey(route.boardId, ticketId)
          : updateDedupeKey(providerEventType, route.boardId, ticketId, observedAt, stateValue(currentState), normalizedFields, activity),
        stableId: action !== 'updated',
        extensions: { ...extensions, provider_event_type: providerEventType },
        data: action === 'created'
          ? { ...common, trigger_source: triggerSource }
          : {
              ...common,
              previous_phase: stateValue(previousState),
              previous_tp_band: tpBand(previousState),
              changed_fields: normalizedFields,
              trigger_source: triggerSource,
            },
      },
    };
  }

  // issue_comment created/commented
  const issue = data.issue;
  const ticket = record(issue);
  const ticketId = entityId(issue) ?? firstText(data.issue_id);
  const commentId = firstText(data.id);
  if (!ticketId || !commentId) {
    return { status: 'unsupported', reason: 'Plane comment payload carries no issue or comment id', providerEvent: event, action, boardId };
  }
  const providerEventType = PLANE_PROVIDER_EVENT_TYPES.ticketCommented;
  const body = firstText(data.comment_html, data.comment_json, data.body, data.comment) ?? '';
  return {
    status: 'routed',
    route,
    event: {
      canonicalType: canonicalTypeForProviderEvent(providerEventType),
      providerEventType,
      observedAt,
      orderingKey: `task:${route.repo}:${ticketId}`,
      dedupeKey: `${providerEventType}:${route.boardId}:${ticketId}:${commentId}:${observedAt}`,
      stableId: true,
      extensions: { ...extensions, provider_event_type: providerEventType },
      data: {
        ...base,
        ticket_id: ticketId,
        ticket_key: ticketKey(route, ticket),
        provider_event_type: providerEventType,
        comment_id: commentId,
        author_id: entityId(data.created_by ?? data.actor ?? data.updated_by) ?? null,
        body,
        appended_at: observedAt,
        comment: data,
      },
    },
  };
}

function changeSide(identifier: unknown, value: unknown): string {
  const id = firstText(identifier);
  if (id) return id;
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value.trim() : JSON.stringify(value);
}

/** The dedupe key of one ticket update (or transition, or deletion).
 *
 * Plane stamps every activity row of one save with the same `updated_at`, and
 * sends a webhook per row: adding two labels, or changing the assignee and the
 * priority in one PATCH, is several deliveries a millisecond apart with the
 * same time and the same state. A key of (time, state) alone gave them one
 * event id, and Candystore (insert ON CONFLICT (id) DO NOTHING) and the stream's
 * Nats-Msg-Id window kept only the first. The changed fields (sorted) and the
 * activity's old -> new value tell them apart; a redelivery of the same
 * delivery still derives the same key.
 */
export function updateDedupeKey(
  providerEventType: string,
  boardId: string,
  ticketId: string,
  observedAt: string,
  state: string | null,
  fields: string[],
  activityValue: unknown = {},
): string {
  const activity = record(activityValue);
  const change = `${changeSide(activity.old_identifier, activity.old_value)}>${changeSide(activity.new_identifier, activity.new_value)}`;
  return [
    providerEventType,
    boardId,
    ticketId,
    observedAt,
    state ?? '',
    [...fields].sort().join(','),
    change === '>' ? '' : change,
  ].join(':');
}

/** The dedupe key of a ticket's creation fact: (board, ticket, created).
 *
 * `deterministicUuid(createdDedupeKey(board, ticket))` is the event id, and
 * the Nats-Msg-Id, of `repo.task.created` for that ticket, whoever publishes it.
 */
export function createdDedupeKey(boardId: string, ticketId: string): string {
  return `${PLANE_PROVIDER_EVENT_TYPES.ticketCreated}:${boardId}:${ticketId}`;
}

function pick(value: unknown, keys: string[]): Record<string, unknown> | unknown {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value;
  const source = value as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const key of keys) if (Object.prototype.hasOwnProperty.call(source, key)) out[key] = source[key];
  return out;
}

const WEBHOOK_STATE_KEYS = ['id', 'name', 'color', 'group'];
const WEBHOOK_LABEL_KEYS = ['id', 'name', 'color'];
const WEBHOOK_ASSIGNEE_KEYS = ['id', 'email', 'avatar', 'last_name', 'avatar_url', 'first_name', 'display_name'];

/** A Plane REST issue (listed with `expand=state,labels,assignees`) as the
 *  `issue.created` webhook delivery Plane would have sent for it.
 *
 * The reconcile sweep feeds this through `classifyPlaneWebhook`, the same
 * normalizer a delivered webhook goes through, so a recovered fact is the fact
 * the webhook would have produced. The expanded state, labels and assignees are
 * trimmed to the fields Plane's webhook serializer carries; everything else on
 * the issue passes through untouched into `data.ticket`.
 */
export function issueAsWebhookPayload(
  issueValue: unknown,
  route: PlaneProjectRoute,
  triggerSource = 'plane-reconcile',
): Record<string, unknown> {
  const issue = { ...record(issueValue) };
  if (issue.state && typeof issue.state === 'object') issue.state = pick(issue.state, WEBHOOK_STATE_KEYS);
  if (Array.isArray(issue.labels)) issue.labels = issue.labels.map((label) => pick(label, WEBHOOK_LABEL_KEYS));
  if (Array.isArray(issue.assignees)) {
    issue.assignees = issue.assignees.map((assignee) => pick(assignee, WEBHOOK_ASSIGNEE_KEYS));
  }
  if (!firstText(issue.project, issue.project_id)) issue.project = route.boardId;
  return {
    event: 'issue',
    action: 'created',
    webhook_id: null,
    workspace_slug: usableWorkspace(route.workspace) ?? null,
    trigger_source: triggerSource,
    data: issue,
  };
}

/** Back-compatible wrapper: the fact, or null for anything not routable. */
export function normalizePlaneWebhook(
  payloadValue: unknown,
  routes: ReadonlyMap<string, PlaneProjectRoute>,
  receivedAt = new Date().toISOString(),
): NormalizedPlaneEvent | null {
  const result = classifyPlaneWebhook(payloadValue, routes, receivedAt);
  return result.status === 'routed' ? result.event : null;
}

export function planeBindingMatches(
  binding: string,
  envelope: Record<string, unknown>,
): boolean {
  const alias = planeEventBindings.find((candidate) => candidate.value === binding);
  if (!alias || envelope.type !== alias.canonicalType) return false;
  const data = record(envelope.data);
  return data.provider === 'plane' && data.provider_event_type === binding;
}
