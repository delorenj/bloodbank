import { DeliverPolicy, jetstream } from '@nats-io/jetstream';
import { connect } from '@nats-io/transport-node';

import { EVENTS_STREAM } from './jetstream';
import type { NatsConnectionOptions } from './nats';
import type { PlaneProjectRoute } from './plane';

/** Plane ingress reconciliation.
 *
 * The Plane webhook is received by n8n itself, and Plane retries a delivery
 * only on a connection error, never on an HTTP 5xx. A ticket created while n8n
 * is down or restarting (every deploy restarts it) therefore never becomes a
 * `bloodbank.repo.task.created` fact, and nothing downstream (grooming, the
 * chip, Candystore) ever hears of it.
 *
 * The sweep closes that gap from the other side: for every routed board it
 * lists the tickets Plane says were created in a lookback window, asks
 * BLOODBANK_EVENTS which of them already have a creation fact, and hands the
 * rest back to the caller, which publishes each one through the webhook's own
 * normalizer. The creation fact's event id and Nats-Msg-Id are a pure function
 * of (board, ticket, created), so a webhook that lands late and the sweep can
 * race without producing two facts.
 */

export const CREATED_SUBJECT = 'bloodbank.evt.repo.task.created';

export const RECONCILE_DEFAULTS = {
  /** How far back to look for tickets Plane created. */
  lookbackMs: 6 * 60 * 60 * 1000,
  /** A ticket younger than this is left to its webhook, which may be in flight. */
  settleMs: 2 * 60 * 1000,
  /** Plane issue pages (newest first) read per board before giving up on reaching the window start. */
  maxPagesPerBoard: 3,
  perPage: 100,
  /** Gap between Plane requests, so a sweep never bursts the API key's budget. */
  paceMs: 250,
  /** Stop the sweep while the API key still has this many requests left this minute. */
  rateReserve: 10,
  /** Facts are searched from this long before the window, for clock skew. */
  factMarginMs: 10 * 60 * 1000,
  /** Board order rotates once per slot, so a sweep cut short still covers every board over time. */
  slotMs: 10 * 60 * 1000,
  /** Upper bound on creation facts read from the stream in one sweep. */
  maxFacts: 20_000,
  /** Creation facts one sweep publishes; the rest wait for the next sweep. */
  maxRecoveries: 20,
};

export interface PlaneIssue {
  id?: string;
  created_at?: string;
  sequence_id?: number;
  name?: string;
  state?: unknown;
  is_draft?: boolean;
  archived_at?: string | null;
  deleted_at?: string | null;
  [key: string]: unknown;
}

export interface PlaneProject {
  id?: string;
  identifier?: string;
  archived_at?: string | null;
  [key: string]: unknown;
}

export interface PlanePage<T> {
  results: T[];
  nextCursor: string | null;
  more: boolean;
}

/** The two Plane reads a sweep needs, behind a seam tests can fake. */
export interface PlaneReader {
  projects(workspace: string): Promise<PlaneProject[]>;
  issuesPage(workspace: string, boardId: string, cursor: string | null): Promise<PlanePage<PlaneIssue>>;
}

/** Plane refused (429) or the key is down to its reserve: stop, try next sweep. */
export class PlaneRateLimited extends Error {}

export interface PlaneReaderOptions {
  baseUrl?: string;
  /** The API key header, as an n8n Header Auth credential stores it. */
  header: { name: string; value: string };
  perPage?: number;
  paceMs?: number;
  rateReserve?: number;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Plane's REST API with pacing and a rate-limit reserve.
 *
 * Plane throttles per API key (60/minute by default) and the key is shared with
 * the chip lane, so the reader spaces its requests and stops as soon as the
 * key's remaining budget reaches the reserve, rather than spending it all and
 * leaving the chip to eat the 429s.
 */
export function planeReader(options: PlaneReaderOptions): PlaneReader {
  const base = (options.baseUrl || 'https://plane.delo.sh').replace(/\/+$/, '');
  const perPage = options.perPage ?? RECONCILE_DEFAULTS.perPage;
  const paceMs = options.paceMs ?? RECONCILE_DEFAULTS.paceMs;
  const reserve = options.rateReserve ?? RECONCILE_DEFAULTS.rateReserve;
  const timeoutMs = options.timeoutMs ?? 15_000;
  const doFetch = options.fetchImpl ?? fetch;
  const sleep = options.sleep ?? defaultSleep;
  const now = options.now ?? Date.now;
  const headerName = text(options.header?.name);
  const headerValue = text(options.header?.value);
  if (!headerName || !headerValue) throw new Error('the Plane API credential has no header name or value');
  let last = 0;
  let exhausted: string | undefined;

  const get = async (path: string): Promise<Record<string, unknown>> => {
    if (exhausted) throw new PlaneRateLimited(exhausted);
    const wait = last + paceMs - now();
    if (wait > 0) await sleep(wait);
    last = now();
    const response = await doFetch(`${base}${path}`, {
      headers: { [headerName]: headerValue, Accept: 'application/json', 'User-Agent': 'Mozilla/5.0' },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 429) {
      exhausted = `Plane answered 429 on ${path.split('?')[0]}`;
      throw new PlaneRateLimited(exhausted);
    }
    if (!response.ok) throw new Error(`Plane GET ${path.split('?')[0]} answered HTTP ${response.status}`);
    const remaining = Number(response.headers.get('x-ratelimit-remaining'));
    if (response.headers.get('x-ratelimit-remaining') !== null && Number.isFinite(remaining) && remaining <= reserve) {
      exhausted = `Plane API key is down to ${remaining} requests this minute (reserve ${reserve})`;
    }
    return record(await response.json());
  };

  const page = <T>(body: Record<string, unknown>): PlanePage<T> => ({
    results: Array.isArray(body.results) ? (body.results as T[]) : [],
    nextCursor: text(body.next_cursor) ?? null,
    more: body.next_page_results === true,
  });

  return {
    async projects(workspace) {
      const projects: PlaneProject[] = [];
      let cursor: string | null = null;
      for (let pages = 0; pages < 10; pages++) {
        const query = `per_page=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
        const result: PlanePage<PlaneProject> = page(await get(`/api/v1/workspaces/${encodeURIComponent(workspace)}/projects/?${query}`));
        projects.push(...result.results);
        if (!result.more || !result.nextCursor) break;
        cursor = result.nextCursor;
      }
      return projects;
    },
    async issuesPage(workspace, boardId, cursor) {
      const query = [
        'order_by=-created_at',
        `per_page=${perPage}`,
        'expand=state,labels,assignees',
        ...(cursor ? [`cursor=${encodeURIComponent(cursor)}`] : []),
      ].join('&');
      return page(await get(
        `/api/v1/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(boardId)}/issues/?${query}`,
      ));
    },
  };
}

/** Ticket ids that already have a `repo.task.created` fact on the bus since `since`.
 *
 * Read straight from BLOODBANK_EVENTS (the source every consumer, Candystore
 * included, is fed from) with a throwaway ordered consumer filtered to the one
 * subject, so the server does the filtering and nothing durable is left behind.
 * A fact from any producer counts: the question is "is this creation on the
 * bus", not "who put it there".
 */
export async function createdTicketIdsSince(
  since: Date,
  options: NatsConnectionOptions & { maxFacts?: number } = {},
  connectNats: typeof connect = connect,
): Promise<Set<string>> {
  const host = options.host || 'localhost';
  const servers = host.includes('://') ? host : `nats://${host}:${options.port ?? 4222}`;
  const maxFacts = options.maxFacts ?? RECONCILE_DEFAULTS.maxFacts;
  const connection = await connectNats({
    servers,
    name: 'n8n-plane-reconcile',
    timeout: options.timeoutMs ?? 5000,
  });
  try {
    const consumer = await jetstream(connection).consumers.get(EVENTS_STREAM, {
      filter_subjects: [CREATED_SUBJECT],
      deliver_policy: DeliverPolicy.StartTime,
      opt_start_time: since.toISOString(),
    });
    try {
      const ids = new Set<string>();
      let pending = (await consumer.info()).num_pending;
      if (pending > maxFacts) {
        throw new Error(`${pending} creation facts since ${since.toISOString()} exceeds the ${maxFacts} a sweep reads`);
      }
      while (pending > 0) {
        const batch = await consumer.fetch({ max_messages: Math.min(256, pending), expires: 5000 });
        let received = 0;
        for await (const message of batch) {
          received += 1;
          pending = message.info.pending;
          const id = createdFactTicketId(message.data);
          if (id) ids.add(id);
        }
        if (!received) {
          throw new Error(`JetStream delivered nothing while ${pending} creation facts were pending`);
        }
      }
      return ids;
    } finally {
      await consumer.delete().catch(() => undefined);
    }
  } finally {
    await connection.close();
  }
}

/** The ticket a stored creation fact is about, or undefined for anything unreadable. */
export function createdFactTicketId(raw: Uint8Array | string): string | undefined {
  try {
    const envelope = record(JSON.parse(typeof raw === 'string' ? raw : Buffer.from(raw).toString('utf8')));
    const data = record(envelope.data);
    return text(data.ticket_id) ?? text(data.task_id);
  } catch {
    return undefined;
  }
}

export interface BoardReport {
  board_id: string;
  board_key: string | null;
  repo: string;
  workspace: string;
  status: 'checked' | 'skipped' | 'unchecked' | 'error';
  reason?: string;
  /** Tickets read from Plane for this board. */
  listed: number;
  /** Tickets created inside the window. */
  in_window: number;
  /** The window start was not reached within the page budget. */
  truncated?: boolean;
}

export interface ReconcileCandidate {
  route: PlaneProjectRoute;
  issue: PlaneIssue;
}

export interface ReconcilePlan {
  window: { from: string; to: string; facts_since: string };
  candidates: ReconcileCandidate[];
  boards: BoardReport[];
  counts: {
    routed_boards: number;
    boards_checked: number;
    in_window: number;
    already_on_bus: number;
    too_young: number;
    skipped_closed: number;
    skipped_draft: number;
    missing: number;
  };
  /** The sweep stopped before every board was read (rate limit reserve or 429). */
  partial: boolean;
  stopped_reason?: string;
}

export interface PlanReconcileOptions {
  routes: ReadonlyMap<string, PlaneProjectRoute>;
  plane: PlaneReader;
  /** Which of these tickets already have a creation fact (by ticket id). */
  knownTicketIds: (since: Date) => Promise<Set<string>>;
  now?: Date;
  lookbackMs?: number;
  settleMs?: number;
  maxPagesPerBoard?: number;
  factMarginMs?: number;
  slotMs?: number;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function workspaceSlug(value: string | undefined): string | undefined {
  const candidate = text(value);
  if (!candidate || candidate === 'unknown' || UUID.test(candidate)) return undefined;
  return candidate;
}

function stateGroup(state: unknown): string | undefined {
  return text(record(state).group)?.toLowerCase();
}

const CLOSED_GROUPS = new Set(['completed', 'cancelled', 'canceled']);

/** Every routed board, rotated by time slot so a partial sweep still covers them all over time. */
export function sweepOrder(
  routes: ReadonlyMap<string, PlaneProjectRoute>,
  now: number,
  slotMs = RECONCILE_DEFAULTS.slotMs,
): PlaneProjectRoute[] {
  const ordered = [...routes.values()].sort((a, b) => a.boardId.localeCompare(b.boardId));
  if (!ordered.length) return ordered;
  const offset = Math.floor(now / Math.max(1, slotMs)) % ordered.length;
  return [...ordered.slice(offset), ...ordered.slice(0, offset)];
}

/** Work out which tickets Plane created in the window that the bus never heard about.
 *
 * Archived boards, boards Plane no longer lists (deleted, or not visible to the
 * key) and routes with no workspace slug are skipped and reported. Drafts and
 * tickets already closed are skipped too: a ticket created and closed while
 * n8n was down needs no grooming. Tickets younger than the settle time are left
 * to a webhook that may still be in flight. Plane is read before the bus, so a
 * fact published while the sweep lists Plane is still seen.
 */
export async function planReconcile(options: PlanReconcileOptions): Promise<ReconcilePlan> {
  const now = (options.now ?? new Date()).valueOf();
  const lookbackMs = options.lookbackMs ?? RECONCILE_DEFAULTS.lookbackMs;
  const settleMs = options.settleMs ?? RECONCILE_DEFAULTS.settleMs;
  const maxPages = Math.max(1, options.maxPagesPerBoard ?? RECONCILE_DEFAULTS.maxPagesPerBoard);
  const factMarginMs = options.factMarginMs ?? RECONCILE_DEFAULTS.factMarginMs;
  const from = now - lookbackMs;
  const to = now - settleMs;
  const factsSince = new Date(from - factMarginMs);

  const boards: BoardReport[] = [];
  const inWindow: ReconcileCandidate[] = [];
  const projectCache = new Map<string, Promise<PlaneProject[]>>();
  let tooYoung = 0;
  let stopped: string | undefined;

  for (const route of sweepOrder(options.routes, now, options.slotMs)) {
    const report: BoardReport = {
      board_id: route.boardId,
      board_key: route.boardKey ?? null,
      repo: route.repo,
      workspace: route.workspace,
      status: 'unchecked',
      listed: 0,
      in_window: 0,
    };
    boards.push(report);
    if (stopped) continue;
    const workspace = workspaceSlug(route.workspace);
    if (!workspace) {
      report.status = 'skipped';
      report.reason = 'route has no workspace slug';
      continue;
    }
    try {
      if (!projectCache.has(workspace)) projectCache.set(workspace, options.plane.projects(workspace));
      const projects = await (projectCache.get(workspace) as Promise<PlaneProject[]>);
      const project = projects.find((candidate) => candidate.id === route.boardId);
      if (!project) {
        report.status = 'skipped';
        report.reason = `Plane does not list board ${route.boardId} in workspace ${workspace} (deleted or not visible)`;
        continue;
      }
      if (text(project.archived_at)) {
        report.status = 'skipped';
        report.reason = `board archived at ${project.archived_at}`;
        continue;
      }
      let cursor: string | null = null;
      let reachedStart = false;
      let pages = 0;
      for (; pages < maxPages; pages++) {
        const page: PlanePage<PlaneIssue> = await options.plane.issuesPage(workspace, route.boardId, cursor);
        for (const issue of page.results) {
          report.listed += 1;
          const created = Date.parse(String(issue.created_at ?? ''));
          if (Number.isNaN(created)) continue;
          if (created < from) {
            reachedStart = true;
            break;
          }
          if (created > to) {
            tooYoung += 1;
            continue;
          }
          report.in_window += 1;
          inWindow.push({ route, issue });
        }
        if (reachedStart || !page.more || !page.nextCursor) {
          reachedStart = true;
          break;
        }
        cursor = page.nextCursor;
      }
      if (!reachedStart) report.truncated = true;
      report.status = 'checked';
    } catch (error) {
      if (error instanceof PlaneRateLimited) {
        stopped = error.message;
        report.status = 'unchecked';
        report.reason = error.message;
        continue;
      }
      report.status = 'error';
      report.reason = (error as Error).message;
    }
  }

  const counts = {
    routed_boards: boards.length,
    boards_checked: boards.filter((board) => board.status === 'checked').length,
    in_window: inWindow.length,
    already_on_bus: 0,
    too_young: tooYoung,
    skipped_closed: 0,
    skipped_draft: 0,
    missing: 0,
  };
  const candidates: ReconcileCandidate[] = [];
  if (inWindow.length) {
    const known = await options.knownTicketIds(factsSince);
    for (const candidate of inWindow) {
      const { issue } = candidate;
      if (!text(issue.id)) continue;
      if (known.has(String(issue.id))) {
        counts.already_on_bus += 1;
        continue;
      }
      if (issue.is_draft === true || text(issue.deleted_at)) {
        counts.skipped_draft += 1;
        continue;
      }
      if (text(issue.archived_at) || CLOSED_GROUPS.has(stateGroup(issue.state) ?? '')) {
        counts.skipped_closed += 1;
        continue;
      }
      candidates.push(candidate);
    }
  }
  counts.missing = candidates.length;

  return {
    window: { from: new Date(from).toISOString(), to: new Date(to).toISOString(), facts_since: factsSince.toISOString() },
    candidates,
    boards,
    counts,
    partial: Boolean(stopped),
    ...(stopped ? { stopped_reason: stopped } : {}),
  };
}

/** The candidates one sweep publishes, oldest first, and the ones it leaves.
 *
 * A newly enrolled board, or a long outage, can surface dozens of missing
 * creations at once, and each one publishes a fact that dispatches a grooming
 * turn and pages ntfy. The cap spreads them over sweeps: a deferred ticket is
 * still missing next time, so the next sweep finds it again. Oldest first, so
 * the ticket closest to leaving the lookback window is not the one left behind.
 */
export function recoveryBatch(
  candidates: ReconcileCandidate[],
  max = RECONCILE_DEFAULTS.maxRecoveries,
): { batch: ReconcileCandidate[]; deferred: ReconcileCandidate[] } {
  const created = (candidate: ReconcileCandidate): number => {
    const at = Date.parse(String(candidate.issue.created_at ?? ''));
    return Number.isNaN(at) ? Number.POSITIVE_INFINITY : at;
  };
  const ordered = [...candidates].sort((a, b) => created(a) - created(b));
  const limit = Math.max(0, Math.floor(max));
  return { batch: ordered.slice(0, limit), deferred: ordered.slice(limit) };
}

/** A candidate's human key (BOARD-12) when the route knows the board key, else its id. */
export function candidateTicketKey(candidate: ReconcileCandidate): string {
  const sequence = candidate.issue.sequence_id;
  if (candidate.route.boardKey && sequence !== undefined && sequence !== null && String(sequence).trim()) {
    return `${candidate.route.boardKey}-${sequence}`;
  }
  return String(candidate.issue.id ?? '');
}
