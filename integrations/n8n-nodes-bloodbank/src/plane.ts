export interface PlaneProjectRoute {
  boardId: string;
  repo: string;
  slug: string;
  workspace: string;
  boardKey?: string;
}

export interface PlaneEventBinding {
  name: string;
  value: string;
  canonicalType: string;
  description: string;
}

export const planeEventBindings: PlaneEventBinding[] = [
  {
    name: 'On Board Created (plane.board.created)',
    value: 'plane.board.created',
    canonicalType: 'bloodbank.v1.repo.board.created',
    description: 'Plane project creation normalized to repo.board.created',
  },
  {
    name: 'On Ticket Created (plane.ticket.created)',
    value: 'plane.ticket.created',
    canonicalType: 'bloodbank.v1.repo.task.created',
    description: 'Plane issue creation normalized to repo.task.created',
  },
  {
    name: 'On Ticket Updated (plane.ticket.updated)',
    value: 'plane.ticket.updated',
    canonicalType: 'bloodbank.v1.repo.task.updated',
    description: 'Non-state Plane issue update normalized to repo.task.updated',
  },
  {
    name: 'On Ticket Transitioned (plane.ticket.transitioned)',
    value: 'plane.ticket.transitioned',
    canonicalType: 'bloodbank.v1.repo.task.updated',
    description: 'Plane issue state transition normalized to repo.task.updated',
  },
  {
    name: 'On Ticket Commented (plane.ticket.commented)',
    value: 'plane.ticket.commented',
    canonicalType: 'bloodbank.v1.repo.task.appended',
    description: 'Plane issue comment normalized to repo.task.appended',
  },
  {
    name: 'On Ticket Deleted (plane.ticket.deleted)',
    value: 'plane.ticket.deleted',
    canonicalType: 'bloodbank.v1.repo.task.updated',
    description: 'Plane issue deletion normalized to a terminal repo.task.updated fact',
  },
];

export interface NormalizedPlaneEvent {
  canonicalType: string;
  providerEventType: string;
  data: Record<string, unknown>;
  extensions: Record<string, string>;
  orderingKey: string;
  dedupeKey: string;
  observedAt: string;
}

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
    });
  }
  return routes;
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

function workspaceFromEntity(value: unknown): string | undefined {
  if (typeof value !== 'object' || value === null) return firstText(value);
  const workspace = value as Record<string, unknown>;
  return firstText(workspace.slug, workspace.name, workspace.id);
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

/** Normalize one Plane webhook into exactly one provider-neutral Bloodbank fact.
 *
 * Plane names remain available as data.provider_event_type and as n8n trigger
 * aliases. They intentionally do not enter CloudEvents type/subject tokens.
 */
export function normalizePlaneWebhook(
  payloadValue: unknown,
  routes: ReadonlyMap<string, PlaneProjectRoute>,
  receivedAt = new Date().toISOString(),
): NormalizedPlaneEvent | null {
  const payload = record(payloadValue);
  const event = firstText(payload.event)?.toLowerCase();
  const action = normalizeAction(payload.action);
  const data = record(payload.data);
  if (!event || !Object.keys(data).length) return null;

  const rawProject = data.project ?? data.project_id ?? payload.project;
  const boardId = event === 'project'
    ? firstText(data.id, data.project_id)
    : entityId(rawProject);
  if (!boardId) return null;

  let route = routes.get(boardId);
  if (!route && event === 'project' && action === 'created') {
    const workspace = workspaceFromEntity(data.workspace ?? payload.workspace) ?? 'unknown';
    const slug = slugify(firstText(data.slug, data.identifier, data.name) ?? boardId);
    route = {
      boardId,
      repo: slug,
      slug,
      workspace,
      boardKey: firstText(data.identifier),
    };
  }
  if (!route) return null;

  const observedAt = normalizeTimestamp(
    data.updated_at ?? data.created_at ?? payload.timestamp ?? payload.created_at,
    receivedAt,
  );
  const base = {
    repo: route.repo,
    slug: route.slug,
    workspace: route.workspace,
    board_id: route.boardId,
    project_id: route.boardId,
    provider: 'plane',
  };
  const extensions = {
    workspace: route.workspace,
    board_id: route.boardId,
    slug: route.slug,
  };

  if (event === 'project' && action === 'created') {
    const providerEventType = 'plane.board.created';
    return {
      canonicalType: 'bloodbank.v1.repo.board.created',
      providerEventType,
      observedAt,
      orderingKey: `board:${route.boardId}`,
      dedupeKey: `${providerEventType}:${route.boardId}:${observedAt}`,
      extensions: { ...extensions, provider_event_type: providerEventType },
      data: {
        ...base,
        board_key: route.boardKey ?? null,
        provider_event_type: providerEventType,
        timestamp: observedAt,
        board: data,
      },
    };
  }

  if (event === 'issue') {
    const ticketId = firstText(data.id);
    if (!ticketId) return null;
    const fields = changedFields(payload, data);
    const activity = record(payload.activity ?? data.activity);
    const isTransition = action === 'updated' && fields.some((field) => field === 'state' || field === 'state_id');
    const providerEventType = action === 'created'
      ? 'plane.ticket.created'
      : action === 'deleted'
        ? 'plane.ticket.deleted'
        : isTransition
          ? 'plane.ticket.transitioned'
          : 'plane.ticket.updated';
    const canonicalType = action === 'created'
      ? 'bloodbank.v1.repo.task.created'
      : 'bloodbank.v1.repo.task.updated';
    const currentState = data.state_detail ?? data.state;
    const previousState = activity.old_value ?? activity.previous_value;
    const normalizedFields = action === 'deleted' && !fields.length ? ['deleted'] : fields;
    const key = ticketKey(route, data);
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
      canonicalType,
      providerEventType,
      observedAt,
      orderingKey: `task:${route.repo}:${ticketId}`,
      dedupeKey: `${providerEventType}:${route.boardId}:${ticketId}:${observedAt}:${stateValue(currentState) ?? ''}`,
      extensions: { ...extensions, provider_event_type: providerEventType },
      data: action === 'created'
        ? common
        : {
            ...common,
            previous_phase: stateValue(previousState),
            previous_tp_band: tpBand(previousState),
            changed_fields: normalizedFields,
            trigger_source: firstText(payload.trigger_source, data.trigger_source) ?? 'plane-webhook',
          },
    };
  }

  if (event === 'issue_comment' && ['created', 'commented'].includes(action)) {
    const issue = data.issue;
    const ticket = record(issue);
    const ticketId = entityId(issue) ?? firstText(data.issue_id);
    const commentId = firstText(data.id);
    if (!ticketId || !commentId) return null;
    const providerEventType = 'plane.ticket.commented';
    const body = firstText(data.comment_html, data.comment_json, data.body, data.comment) ?? '';
    return {
      canonicalType: 'bloodbank.v1.repo.task.appended',
      providerEventType,
      observedAt,
      orderingKey: `task:${route.repo}:${ticketId}`,
      dedupeKey: `${providerEventType}:${route.boardId}:${ticketId}:${commentId}:${observedAt}`,
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
    };
  }

  return null;
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
