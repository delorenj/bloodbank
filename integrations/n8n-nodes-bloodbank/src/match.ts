/** "Only When Data Matches": drop a message before it becomes an execution.
 *
 * A condition names a dot path into the full envelope (`data.context.reason`,
 * `type`, `data.provider`) and a list of accepted values. A message passes when
 * every condition holds. An empty value list means "present and non-empty".
 * Arrays match when any element does; everything compares as trimmed text.
 */
export interface DataCondition {
  path: string;
  values: string[];
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function splitList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((entry) => String(entry ?? '').trim()).filter(Boolean);
  }
  return String(value ?? '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

/** Read the n8n fixedCollection `{ conditions: [{ path, values }] }`. */
export function parseDataConditions(parameter: unknown): DataCondition[] {
  const root = record(parameter);
  const rows = root && Array.isArray(root.conditions) ? root.conditions : [];
  const conditions: DataCondition[] = [];
  for (const row of rows) {
    const entry = record(row);
    if (!entry) continue;
    const path = String(entry.path ?? '').trim().replace(/^\$json\./, '');
    if (!path) continue;
    conditions.push({ path, values: splitList(entry.values) });
  }
  return conditions;
}

export function valueAtPath(root: unknown, path: string): unknown {
  let current: unknown = root;
  for (const segment of path.split('.').filter(Boolean)) {
    if (Array.isArray(current) && /^\d+$/.test(segment)) {
      current = current[Number(segment)];
      continue;
    }
    const container = record(current);
    if (!container) return undefined;
    current = container[segment];
  }
  return current;
}

function scalarText(value: unknown): string | undefined {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return undefined;
}

export function conditionHolds(envelope: unknown, condition: DataCondition): boolean {
  const found = valueAtPath(envelope, condition.path);
  const candidates = (Array.isArray(found) ? found : [found])
    .map(scalarText)
    .filter((entry): entry is string => entry !== undefined && entry !== '');
  if (!condition.values.length) return candidates.length > 0;
  return candidates.some((candidate) => condition.values.includes(candidate));
}

export function matchesDataConditions(envelope: unknown, conditions: DataCondition[]): boolean {
  return conditions.every((condition) => conditionHolds(envelope, condition));
}
