/** Scope routing for a Plane board that several projects (or agents) share.
 *
 * A board is normally owned by one project. When several own it, a ticket is
 * routed by the label it carries: each owner declares the label(s) it takes
 * (`scope_label`), and one owner may declare itself the board's default
 * (`scope_default`) for tickets whose labels match none of them.
 *
 * The declarations live in config, never here: this module knows no label
 * vocabulary (`repo:*` is a convention of the boards, not of the code), and it
 * never lets iteration order pick an owner. Every choice is a pure function of
 * the declarations and the ticket's labels.
 */

export interface Scoped {
  /** Lowercased label names this owner takes. */
  scopeLabels?: string[];
  /** This owner takes tickets that carry no matching scope label. */
  scopeDefault?: boolean;
}

function scopeText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim().toLowerCase() : undefined;
}

/** A `scope_label` declaration: one label or a list of them. */
export function scopeLabelsFrom(value: unknown): string[] | undefined {
  const raw = Array.isArray(value) ? value : [value];
  const labels = [...new Set(raw.map(scopeText).filter((label): label is string => Boolean(label)))].sort();
  return labels.length ? labels : undefined;
}

/** A `scope_default` declaration: only a real boolean true counts. */
export function scopeDefaultFrom(value: unknown): boolean | undefined {
  return value === true ? true : undefined;
}

/** The scope fields of a config block (a pjangler `ticket_provider` or a Hermes
 *  `plane` block), present only when declared so unscoped routes stay unchanged. */
export function scopeFields(block: Record<string, unknown>): Scoped {
  const scopeLabels = scopeLabelsFrom(block.scope_label ?? block.scope_labels);
  const scopeDefault = scopeDefaultFrom(block.scope_default);
  return {
    ...(scopeLabels ? { scopeLabels } : {}),
    ...(scopeDefault ? { scopeDefault } : {}),
  };
}

/** Label names from a Plane payload's `labels`: objects ({name}) or bare names. */
export function labelNames(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const names: string[] = [];
  for (const label of value) {
    const name = scopeText(
      typeof label === 'object' && label !== null ? (label as Record<string, unknown>).name : label,
    );
    if (name) names.push(name);
  }
  return [...new Set(names)].sort();
}

/** Owners in their one canonical order: the declared default first, then by the
 *  caller's tie key. Two defaults, or none, still give one stable answer. */
export function orderOwners<T extends Scoped>(owners: readonly T[], tieKey: (owner: T) => string): T[] {
  return [...owners].sort((a, b) => {
    if (Boolean(a.scopeDefault) !== Boolean(b.scopeDefault)) return a.scopeDefault ? -1 : 1;
    const ka = tieKey(a);
    const kb = tieKey(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
}

/** The owner of one ticket among the owners of a shared board.
 *
 * `ordered` comes from `orderOwners`. An owner whose scope matches one of the
 * ticket's labels wins (the earliest in canonical order when several match);
 * otherwise the board's default, which is the first in canonical order.
 */
export function pickOwner<T extends Scoped>(ordered: readonly T[], labels: readonly string[]): T {
  if (ordered.length === 1 || !labels.length) return ordered[0];
  const wanted = new Set(labels);
  return ordered.find((owner) => owner.scopeLabels?.some((label) => wanted.has(label))) ?? ordered[0];
}
