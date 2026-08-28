<!-- Authored 2026-08-28. Implementation lands mostly in 33GOD/pjangler;
     the consumer defect that surfaced it is in this repo
     (integrations/n8n-nodes-bloodbank/src/plane.ts). -->

> **STATUS 2026-08-28 — the spine (PID-0 … PID-7) is shipped and verified.**
> Measured live, not reported: `routes 24 | blank boardKeys 0`, `ticket_key 33GOD-41`.
> Fleet registry 28 agents / 0 mismatches vs live Plane. SSOT 25 records, 14 linked and
> provider-confirmed, 0 collisions, 0 provenance lies. `npm test` 58/58.
> PID-8/9/11 were not built; PID-10 landed as a surgical field reconciler only.
>
> Three things the build changed about this plan:
> 1. **R3 was wrong.** Trello assigns no identifier, so `linked ⇒ identifier_source==="provider"`
>    made an honest Trello link illegal. The contract now splits `board_confirmed_at`
>    (any provider can confirm a board) from `identifier_source` (only Plane/Linear assign keys).
> 2. **intelliforia is a Trello project**, not a Plane one with a dead board. The plan said to
>    clear it; doing so would have deleted the only correct binding in the file.
> 3. **Both "pre-existing, environmental" test failures were real product bugs** — a missing
>    `SKIP_HOST_STATE` guard that broke every deferred agent render, and a `readFileSync(…,"utf8")`
>    that silently rewrote operator config with U+FFFD. Neither needed quarantine.
>
> Live page: https://claude.ai/code/artifact/16c6cf3e-580d-4b10-98ab-37b7b794222d

# Epic: Provider-Derived Project Identity (PID)

## Problem

Five stores hold a project's board identity and four of them may **invent** it. Measured live this session: Plane workspace `33god` has **69** boards and `automaticai` has **7**; `~/.hermes/agents-registry.yaml` has **30** agents (27 with a `project_id`); `~/.config/pjangler/projects.yaml` has **25** projects, **22** of which carry `board_id: ""`. Ten of the 27 bound agents hold an identifier Plane disagrees with — `holocene HOLPM≠HOLOC`, `voxxy VOXPM≠VOXXY`, `skillex SKIPM≠SKRILL`, `nautilus-trader NAUPM≠MKTJNG`, `candybar CANPM≠CANDYS`, `delodocs DELPM≠DOCS`, `ssbnk SSBN≠SSBNK`, `zshyzsh ZSHPM≠ZSHYZH`, `heyma heyma≠HEYMA`, `pjangler ''≠PJAN` — and two more (`coachingagentframework d7f7b5f6…`, `tonnybox b9016a74…`) point at boards that 404 in **both** workspaces.

Four sites mint the fiction. `bloodbank/agents/hermes/pm/.scripts/40-plane.sh:14-16` computes `IDENT="${RAW:0:5}"` from `${REPO:0:3}${ROLE:0:2}`, then at `:25-32` searches Plane for a project whose *identifier* equals that invention — so a board that already exists as `HOLOC` is structurally invisible to a search for `HOLPM` and a duplicate gets created — parses only `d.get('id')` at `:44`, and persists the fiction at `:49` via `yaml_set plane.identifier "$IDENT"`. `42-ticket-provider.sh:156` repeats it with `IDENT="${SOT_IDENT:-${RAW:0:4}}"`. `pjangler/src/project/index.ts:929` `deriveProjectIdentifier()` returns `compact.slice(0,4) || "PROJ"`, consumed at `:1016`; `src/mcp-server.ts:580` inlines `projectSlug.slice(0, 4).toUpperCase()`. Nothing reads the identifier back: `TicketProviderBoardResult` (`src/project/index.ts:711`) has no identifier field, the create-board parse at `:843-857` picks `board_id`/`board_url` and drops it, and `linkTicketProviderBoard` (`:1172`) writes `action.identifier` — the proposal — into both `projects.yaml` and `.project.json`.

The live consequence: `integrations/n8n-nodes-bloodbank/src/plane.ts:81` sets `boardKey = plane.identifier` and `ticketKey()` at `:178-184` returns `null` when `boardKey` is empty. The pjangler board published `ticket_key: null` (n8n execs 222600/222601), and holocene publishes `HOLPM-12` for a ticket Plane calls `HOLOC-12`.

## What already exists

We are not rebuilding any of this.

- **`ProjectRecord` / `ProjectTicketProvider` typed contract** — `src/project/index.ts:47`, `:71`. `board_id` and `identifier` are already optional fields with a `state` enum.
- **`validateProjectRegistry()`** — `src/project/index.ts:446`. Already enforces schema_version, unique slug, slug===key, unique resolved `repo_path`, unique `notebook_id`, unique `overview_note_id`, and — the shape we copy — `notebook.state==="linked" ⇒ notebook_id && overview_note_id` at `:1588`.
- **`RegistryStore`** — `src/project/RegistryStore.ts:40`, with `YamlRegistryStore` (`:51`), `PgRegistryStore` (`:110`), `DualWriteRegistryStore` (`:457`, YAML-authoritative, best-effort PG).
- **CST-preserving writes** — `mergeYamlMapping` + atomic `saveProjectRegistry` (`src/project/index.ts:365`, `:400-444`).
- **`pj init` already emits `registry.upsert`** — `src/project/index.ts:1090`, handled at `src/index.ts:182`, dry-run by default. State is already derived as `board_id ? "linked" : "planned"` at `:1123`.
- **Plane HTTP client with credential resolution** — `src/project/boardQuery.ts:168-360`: `planeBase()` honoring `PLANE_BASE`, `resolvePlaneApiKey()` which reads `~/.hermes/fleet.env` and resolves `op://` refs via `op read`, per-workspace `PLANE_<WS>_API_KEY` derivation, cursor pagination, 401/403 classification, `PJANGLER_BOARD_TIMEOUT_MS`.
- **The Plane adapter read-back is already written** — `pjangler/templates/hermes-agent/template/.scripts/providers/plane.sh:171-176,278-289` already parses `LIVE_IDENTIFIER` off the create response *and* the detail lookup, and `die`s when Plane omits it. Bloodbank's copy (`agents/hermes/pm/.scripts/providers/plane.sh`, 314 lines vs 329) is simply **stale**.
- **CLI plumbing** — `pj project init|list|show|doctor` at `src/index.ts:773-827`, with `--registry <path>` and `--json` on doctor; `PJ_PROJECT_REGISTRY` env override at `src/project/index.ts:25`.
- **`pj` on PATH is a symlink to `dist/index.js`** — `npm run build` genuinely refreshes it. There is no stale-global-build problem.

## Decision

| store | decision | rationale |
|---|---|---|
| **Plane REST API** | **Sole authority for `identifier` and `board_id`.** Read back on every create, link, and reconcile. Never authored, never guessed, never derived from a slug. | Plane assigns the identifier and may normalize it. All 10 measured mismatches exist because a local guess was written where a read-back belonged. |
| **`~/.config/pjangler/projects.yaml`** | **SSOT for registration.** `pj init` is the ingress. Gains `identifier_source`, `identifier_fetched_at`, `aliases[]`, and five validator rules. Mirrors the provider; mirrors `.project.json`. | The user's decision. The typed record, the validator, three store backends, and the `registry.upsert` action all already exist — the gap is three missing rules, not a missing subsystem. Not in a git repo, so the validator is the safety net. |
| **`<repo>/.project.json`** | **Authoritative-on-read for the board binding. Never bulk-rewritten.** The registry mirrors it; disagreement is drift. | `src/describe/index.ts:389-391` already states this contract. 10 of the 22 empty `board_id`s are recoverable from these files — they are an *adoption source*, not a migration target (A1). |
| **`~/.hermes/agents-registry.yaml`** | **Derived projection.** `pj project identity --apply` becomes the repair-capable writer for the `plane:` block. The two existing writers keep inserting, now sourcing provider-derived values. | This is the file n8n reads, uncached, per execution (`PlaneBloodbank.node.ts:195`) — fixing it fixes production with no rebuild and no restart. Neither existing writer can repair: `80-registry.sh:40` runs only as provisioning step 80, and `src/parity/rules.ts:551` bails with `if (current.includes(agentId)) return null`. |
| **`<role_dir>/role.yaml`** | **Derived projection, agent-local.** `plane.identifier` becomes write-once-from-provider. | It is the input to *both* hermes-registry writers (`80-registry.sh:40` reads `yaml_get plane.identifier`; `rules.ts:553` reads `yamlGet(text,"plane.identifier")`). Fixing the value here corrects both without touching `80-registry.sh`'s 32-positional argv (A6). |
| **CommonProject `copier.yml` `project_identifier`** | **Demoted to a proposal.** Renders alongside `"identifier_source": "proposed"`. | A scaffolded repo must not be able to claim provider provenance it does not have. |

## The contract

```ts
// src/project/index.ts:47 — ProjectTicketProvider gains two fields
export interface ProjectTicketProvider {
  type: SupportedTicketProvider | string;
  workspace?: string;
  identifier?: string;
  board_id?: string;
  /** Legacy input only. New manifests derive board URLs from provider/workspace/board_id. */
  board_url?: string;
  state?: "planned" | "linked" | "skipped" | string;

  /** NEW. "provider" means this exact string came back from a provider create/link/describe
   *  response. "proposed" means a client suggested it and no provider has confirmed it.
   *  Only "provider" may accompany state:"linked". */
  identifier_source?: "provider" | "proposed";
  /** NEW. RFC3339 UTC stamp of the last successful provider read-back. Preserved — never
   *  blanked — when a fetch fails or a board 404s; see degraded[]/unresolved[]. */
  identifier_fetched_at?: string;
}

// src/project/index.ts:71 — ProjectRecord gains one field (PID-8 only)
export interface ProjectRecord {
  // …unchanged…
  ticket_provider: ProjectTicketProvider;
  /** NEW. Extra handles that resolve to this record: bb↔bloodbank, project↔33god,
   *  without renaming a slug any hardcoded consumer depends on. */
  aliases?: string[];
  agents: Record<string, ProjectAgentRecord>;
}

// src/project/index.ts:322,327 — owned-key lists. REQUIRED, not cosmetic: mergeYamlMapping
// (:365-382) deletes only owned keys absent from the desired value, so an unlisted key
// survives forever as a stale ghost — and an unlisted NEW key is silently dropped on
// the next registry.upsert, un-stamping every record PID-2 just stamped.
const PROJECT_REGISTRY_OWNED_KEYS = [
  "name", "slug", "repo_path", "description", "status", "source_artifacts", "template",
  "ticket_provider", "aliases", "agents", "automation", "notebook", "created_at", "updated_at",
] as const;                                //  ^^^^^^^^^ ADDED (PID-8)
const TICKET_PROVIDER_OWNED_KEYS = [
  "type", "workspace", "identifier", "board_id", "board_url", "state",
  "identifier_source", "identifier_fetched_at",     // ADDED (PID-3)
] as const;

// src/project/index.ts:711 — the adapter result carries the provider's answer
export interface TicketProviderBoardResult {
  ok: boolean; skipped: boolean;
  boardId?: string;
  boardUrl?: string;
  identifier?: string;      // ADDED — parsed from the create_board envelope at :843-857
  logs: string[]; error?: string;
}
```

The validator rules go inside `validateProjectRecord` (`src/project/index.ts:1572`), immediately after the `ticket_provider` `isRecord` guard at `:1582`, mirroring the notebook invariant one screen below at `:1587-1588`:

```ts
  if (!isRecord(project.ticket_provider)) throw new Error(`Project ${key} ticket_provider must be a mapping`);
  const tp = project.ticket_provider as ProjectTicketProvider;

  // R1 — state enum. Unchecked today: the live registry holds intelliforia state:"active".
  if (tp.state !== undefined && !["planned", "linked", "skipped"].includes(tp.state))
    throw new Error(`Project ${key} ticket_provider state is invalid: ${tp.state}`);

  // R2 — provenance enum.
  if (tp.identifier_source !== undefined
      && tp.identifier_source !== "provider" && tp.identifier_source !== "proposed")
    throw new Error(`Project ${key} ticket_provider.identifier_source must be "provider" or "proposed"`);

  // R3 — THE INVARIANT. Exact shape of the notebook rule at :1588.
  if (tp.state === "linked" && (!tp.board_id || !tp.identifier || tp.identifier_source !== "provider"))
    throw new Error(
      `Project ${key} is linked but its identity is not provider-derived ` +
      `(board_id=${tp.board_id || "-"} identifier=${tp.identifier || "-"} source=${tp.identifier_source || "-"}); ` +
      `run: pj project identity ${key} --apply`);
```

And in `validateProjectRegistry` (`:446`), one added Map and one **narrowed** Map:

```ts
  const boardIds = new Map<string, string>();   // R4, declare beside identifiers at :454

  // …inside the loop, after the repo_path block…
  const tp = project.ticket_provider;
  const scope = `${tp.type ?? ""}\u0000${(tp.workspace ?? "").toLowerCase()}`;

  // R4 — board_id uniqueness, scoped. Empty board_id is EXEMPT (22 of 25 records share "").
  // Catches the live intelliforia / intelliforia-mobile collision on 687535e9873b89478afef689.
  const bid = (tp.board_id ?? "").trim();
  if (bid) {
    const prior = boardIds.get(`${scope}\u0000${bid}`);
    if (prior && prior !== slug)
      throw new Error(`Duplicate project board_id: ${bid} used by ${prior} and ${slug}`);
    boardIds.set(`${scope}\u0000${bid}`, slug);
  }

  // R5 — NARROWING, not an addition. Replaces the GLOBAL key at :466-474. The global map is
  // exactly why a second workspace can never link; reproduced live: two records with
  // identifier DUP in workspaces 33god and automaticai throw today.
  const identifier = tp.identifier?.toUpperCase();
  if (identifier) {
    const key5 = `${scope}\u0000${identifier}`;
    const existing = identifiers.get(key5);
    if (existing && existing !== slug)
      throw new Error(`Duplicate project identifier ${identifier} in ${tp.type}/${tp.workspace}: used by ${existing} and ${slug}`);
    identifiers.set(key5, slug);
  }
```

Worked record — `bb` after PID-2 and PID-5, with values confirmed against Plane (`GET /api/v1/workspaces/33god/projects/` returns `identifier: "BB"` for `10d06f8d-…`):

```yaml
projects:
  bb:
    name: Bloodbank
    slug: bb
    repo_path: /home/delorenj/code/33GOD/bloodbank
    aliases: [bloodbank]                      # PID-8
    ticket_provider:
      type: plane
      workspace: 33god
      board_id: 10d06f8d-c110-4ce5-beaa-0914534b090a   # adopted from .project.json (PID-5)
      identifier: BB                                    # read back from Plane (PID-2)
      identifier_source: provider
      identifier_fetched_at: "2026-08-28T00:00:00Z"
      state: linked                                     # now legal: all three present
```

## Ingress & egress

| operation | command | writes / reads |
|---|---|---|
| Register a project (**the only ingress**) | `pj init <name> --apply --no-tui` | **W** `projects.yaml` (`registry.upsert`, `index.ts:1090`) · **W** `<repo>/.project.json` · **R** existing registry for idempotency (`:1057`) |
| Create or link a board | `pj init … --live --apply` → `ticket-provider.create-or-link` (`:1114`) → `providers/plane.sh create_board` (`:804`) | **R** Plane REST · **W** `projects.yaml` + `.project.json` via `linkTicketProviderBoard` (`:1172`), now with `identifier` from the response |
| Learn a board's real identity | `providers/plane.sh describe_board <workspace> <board_id>` | **R** `GET /workspaces/<ws>/projects/<id>/` · **W** nothing (stdout JSON) |
| Reconcile identity across stores | `pj project identity [slug\|--all] [--apply] [--json] [--hermes-registry <path>]` | **R** Plane (both workspaces) · **R** `projects.yaml`, `<repo>/.project.json`, `agents-registry.yaml` · **W** *(only with `--apply`)* `projects.yaml` `ticket_provider.{identifier,identifier_source,identifier_fetched_at,state}` and `agents.<id>.plane.identifier` |
| Adopt an existing binding | `pj project adopt [--from-manifest] [--from-hermes] [--apply] [--json]` | **R** registered repos' `.project.json` + `agents-registry.yaml` joined on `repo_path` · **W** `projects.yaml` `board_id` **only** (never a manifest — A1) |
| Read a project by any handle | `pj project show <slug\|alias\|board_id\|identifier\|repo_path>` | **R** `projects.yaml` · **W** nothing |
| Gate | `pj project doctor [--json] [--strict] [--registry <p>] [--hermes-registry <p>]` | **R** all stores · **W** nothing · non-zero exit on drift under `--strict` |
| Project the fleet registry | `pj registry emit hermes --check\|--apply` *(PID-10)* | **R** registry + Plane · **W** `agents-registry.yaml` atomically, no `.bak-*` |
| Project the n8n route table | `pj registry emit plane-routes --out <path>` *(PID-11)* | **R** registry · **W** the route table JSON |
| Consume (unchanged) | n8n `PlaneBloodbank` node, `.node.ts:195` | **R** `agents-registry.yaml` per execution, uncached |

## Stories

### Spine — ship in this order

---

**PID-0 — Unbreak `npm run build`** (XS, depends-on: none)

Three symbols are used but never imported in `EnsureTemplateConfig.ts`; esbuild bundles them anyway, so `npm run build` emits a CLI whose `pj init --provision-agent` dies with `platform is not defined`. Every later story's test AC is red until this lands.

`touches:` `pjangler/src/commands/hermes/EnsureTemplateConfig.ts`, `pjangler/package.json`

```bash
# BEFORE — three TS2304 errors today
cd /home/delorenj/code/33GOD/pjangler && npx tsc --noEmit 2>&1 | grep -c 'error TS'
# expect today: 3  (EnsureTemplateConfig.ts:50 realpathSync, :115 platform, :450 describePathType)

# AFTER — typecheck is clean and fails the shell when it is not
cd /home/delorenj/code/33GOD/pjangler && npm run typecheck 2>&1 \
  | python3 -c "import sys; e=[l for l in sys.stdin if 'error TS' in l]; assert not e, ''.join(e); print('typecheck clean')"
# expect: typecheck clean

# a freshly built dist still passes the registry suite (it does NOT today, post-build)
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null && node tests/project-registry-regressions.mjs
# expect: project registry regressions passed

# typecheck is now gated, so this class can never bundle clean again
cd /home/delorenj/code/33GOD/pjangler \
  && python3 -c "import json; t=json.load(open('package.json'))['scripts']['test']; assert 'typecheck' in t, t; print('OK typecheck in npm test')"
```

---

**PID-1 — Forward-port the Plane adapter; delete the shell minter everywhere** (S, depends-on: none)

The read-back is already written in `pjangler/templates/hermes-agent/template/.scripts/providers/plane.sh:171-176,278-289` (`LIVE_IDENTIFIER`, with a hard `die` when Plane omits it) and in that tree's `42-ticket-provider.sh`. Bloodbank's copies are 15 and 83 lines behind. Port them, add `describe_board`, and delete the `40-plane.sh` mint — which is byte-identical across every copy on disk.

`touches:` `bloodbank/agents/hermes/pm/.scripts/providers/plane.sh`, `bloodbank/agents/hermes/pm/.scripts/42-ticket-provider.sh`, `bloodbank/agents/hermes/pm/.scripts/40-plane.sh`, `pjangler/templates/hermes-agent/template/.scripts/{40-plane.sh,providers/plane.sh}`, `hermes-agent-template/template/.scripts/{40-plane.sh,providers/plane.sh}`, `33GOD/agents/hermes/pm/.scripts/40-plane.sh`

```bash
# BEFORE — the bloodbank adapter drops the identifier Plane just returned.
# REQUIRES network to plane.delo.sh + an authenticated `op` session (fleet.env holds an op:// ref).
PKEY="$(op read "$(grep -m1 '^PLANE_33GOD_API_KEY=' ~/.hermes/fleet.env | cut -d= -f2- | tr -d '"')")" \
  || { echo 'BLOCKED: 1Password session required'; exit 1; }
# prove the probe is lookup-only and cannot mint a 70th board: an exact-name match must exist
curl -fsS "https://plane.delo.sh/api/v1/workspaces/33god/projects/?per_page=200" -H "X-API-Key: $PKEY" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["results"]; m=[p for p in r if p["name"].strip().lower()=="holocene"]; assert len(m)==1, m; assert m[0]["identifier"]=="HOLOC" and m[0]["id"].startswith("727a2b17"); print("OK oracle HOLOC", m[0]["id"][:8])'
# expect: OK oracle HOLOC 727a2b17

# AFTER — the adapter returns the provider's identifier on the reuse path
cd /home/delorenj/code/33GOD/bloodbank/agents/hermes/pm \
  && PLANE_API_KEY="$PKEY" .scripts/providers/plane.sh create_board 'Holocene' 'HOLPM' 'probe' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["identifier"]=="HOLOC", d; assert d["board_id"].startswith("727a2b17"); print("OK", d["identifier"], d["board_id"][:8])'
# expect: OK HOLOC 727a2b17   (today: KeyError "identifier")

# the new read-only op, workspace as an explicit ARG so .project.json precedence cannot hijack it
env -u PLANE_API_KEY PLANE_API_KEY="$PKEY" \
  /home/delorenj/code/33GOD/bloodbank/agents/hermes/pm/.scripts/providers/plane.sh \
  describe_board 33god 727a2b17-a1dd-46f6-b583-afa5a2d2cdae \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d=={**d,"identifier":"HOLOC","workspace":"33god"}, d; print("OK describe_board", d["identifier"])'
# expect: OK describe_board HOLOC

# the minting arithmetic is gone from EVERY copy on disk, and no local value is persisted
cd /home/delorenj/code/33GOD && for f in $(find . ~/.hermes -name 40-plane.sh -o -name 42-ticket-provider.sh 2>/dev/null); do
  grep -Eq 'RAW=.*\$\{REPO:0:3\}|IDENT="\$\{RAW:0:[45]\}"' "$f" && { echo "MINTS: $f"; exit 1; }
  grep -Eq '(yaml_set|pj_write|mirror_to_role_yaml)[^\n]*"\$\{?IDENT\}?"' "$f" && { echo "PERSISTS MINT: $f"; exit 1; }
done; echo 'OK no minting site in any copy'
# baseline today: 8 identical copies of 40-plane.sh mint at :14-16 and persist at :49

# behavioral proof, offline: a fake curl on PATH drives all three branches against role.yaml
cd /home/delorenj/code/33GOD/bloodbank && bash ops/tests/plane-identifier-readback.sh && echo 'OK read-back is behavioral'
# harness asserts: create -> role.yaml plane.identifier == the stub's identifier (not the proposal);
# reuse -> == the existing board's identifier; stub fails -> pre-existing value UNCHANGED (G7).

# the three trees stay byte-identical, or the parity rules drift
cd /home/delorenj/code/33GOD && FAIL=0
for rel in 40-plane.sh 42-ticket-provider.sh providers/plane.sh; do
  for tree in pjangler/templates/hermes-agent/template/.scripts hermes-agent-template/template/.scripts; do
    diff -q "bloodbank/agents/hermes/pm/.scripts/$rel" "$tree/$rel" >/dev/null || { echo "DRIFT: $rel vs $tree"; FAIL=1; }
  done
done; [ "$FAIL" = 0 ] && echo 'OK parity across all canonical trees'
# baseline today: 42-ticket-provider.sh (204 vs 287) and providers/plane.sh (314 vs 329) DRIFT

# every touched script still parses under the shell that actually runs it
for f in $(find /home/delorenj/code/33GOD/bloodbank/agents/hermes/pm/.scripts -name '*.sh'); do
  case "$(head -1 "$f")" in *bash) bash -n "$f" || exit 1 ;; *) dash -n "$f" || { echo "POSIX FAIL $f (pjangler spawns this with sh)"; exit 1; } ;; esac
done; echo OK
```

---

**PID-2 — `pj project identity`: read identity back, repair both YAML stores** (M, depends-on: PID-0)

One command that resolves each agent's board, asks Plane what it is actually called, and writes the truth into `projects.yaml` and `~/.hermes/agents-registry.yaml`. `--all` iterates the **hermes registry** (30 agents, 27 bound) — not the 25-entry pjangler registry, which contains no `holocene` slug at all. Because `PlaneBloodbank.node.ts:195` re-reads the file uncached on every execution, this fixes production with no `npm run deploy` and no `pm2 restart`.

`touches:` `pjangler/src/project/identity.ts` (new), `pjangler/src/project/index.ts`, `pjangler/src/index.ts`, `pjangler/tests/project-identity-regressions.mjs` (new), `pjangler/package.json`

```bash
# BEFORE — reproduce the live bug: one route has a boardId and no boardKey
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && node -e "
const {planeRoutesFromRegistry}=require('./dist/plane.js');const yaml=require('yaml'),fs=require('fs'),os=require('os');
const r=planeRoutesFromRegistry(yaml.parse(fs.readFileSync(os.homedir()+'/.hermes/agents-registry.yaml','utf8')));
console.log(JSON.stringify(r.get('18a79832-00fb-4146-b054-d88528f9fef3')));
console.log([...r.values()].filter(v=>v.boardId&&!v.boardKey).length,'blank boardKeys');"
# expect today: {"boardId":"18a79832-…","repo":"pjangler","slug":"pjangler","workspace":"33god"}  /  1 blank boardKeys

# board_id is resolved from the repo's .project.json (authoritative-on-read, A1) — the
# registry holds board_id:"" for pjangler. REQUIRES network + an `op` session.
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && node dist/index.js project identity pjangler --json | python3 -c '
import sys,json; d=json.load(sys.stdin); r={p["slug"]:p for p in d["projects"]}["pjangler"]
assert r["board_id"]=="18a79832-00fb-4146-b054-d88528f9fef3", r
assert r["provider_identifier"]=="PJAN", r
assert r["identifier_source"]=="provider", r
print("OK PJAN read back from provider")'

# dry-run is the default and is inert on BOTH stores
cd /home/delorenj/code/33GOD/pjangler \
  && cp ~/.hermes/agents-registry.yaml /tmp/pid2-h.yaml && cp ~/.config/pjangler/projects.yaml /tmp/pid2-p.yaml \
  && node dist/index.js project identity --all >/dev/null \
  && diff -q /tmp/pid2-h.yaml ~/.hermes/agents-registry.yaml \
  && diff -q /tmp/pid2-p.yaml ~/.config/pjangler/projects.yaml && echo 'OK dry-run inert on both stores'

# --apply, then every resolvable agent matches Plane. HARD COUNTS: a no-op run cannot pass.
cd /home/delorenj/code/33GOD/pjangler && cp ~/.hermes/agents-registry.yaml "/tmp/pid2-hermes-$(date +%s).yaml" \
  && node dist/index.js project identity --all --apply >/dev/null && python3 - <<'PY'
import json, os, subprocess, urllib.request, yaml
key = subprocess.run(['op','read', open(os.path.expanduser('~/.hermes/fleet.env')).read().split('PLANE_33GOD_API_KEY=')[1].split('\n')[0].strip('"')],
                     capture_output=True, text=True).stdout.strip()
assert key, 'unlock `op` first'
real = {}
for ws in ('33god','automaticai'):
    req = urllib.request.Request(f'https://plane.delo.sh/api/v1/workspaces/{ws}/projects/?per_page=200',
                                 headers={'X-API-Key': key})
    for p in json.load(urllib.request.urlopen(req))['results']:
        real[p['id']] = p['identifier']
a = yaml.safe_load(open(os.path.expanduser('~/.hermes/agents-registry.yaml')))['agents']
bound = [(k, (v.get('plane') or {}).get('project_id'), (v.get('plane') or {}).get('identifier')) for k, v in a.items() if (v.get('plane') or {}).get('project_id')]
resolvable = [(k, p, i) for k, p, i in bound if p in real]
assert len(a) == 30, len(a)
assert len(bound) == 27, len(bound)
assert len(resolvable) == 25, f'expected 25 resolvable across both workspaces, got {len(resolvable)}'
bad = [(k, i, real[p]) for k, p, i in resolvable if (i or '') != real[p]]
assert not bad, bad
print(f'OK all {len(resolvable)} resolvable agents match Plane')
PY
# expect: OK all 25 resolvable agents match Plane   (10 drift repaired)

# AFTER — the live null is gone, holocene is HOLOC, and the two DEAD boards keep last-good.
# coachingagentframework (d7f7b5f6) and tonnybox (b9016a74) 404 in BOTH workspaces — verified.
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && node -e "
const {planeRoutesFromRegistry}=require('./dist/plane.js');const yaml=require('yaml'),fs=require('fs'),os=require('os');
const r=planeRoutesFromRegistry(yaml.parse(fs.readFileSync(os.homedir()+'/.hermes/agents-registry.yaml','utf8')));
const get=repo=>[...r.values()].find(v=>v.repo===repo);
if(r.get('18a79832-00fb-4146-b054-d88528f9fef3').boardKey!=='PJAN')throw new Error('pjangler');
if(get('holocene').boardKey!=='HOLOC')throw new Error('holocene='+get('holocene').boardKey);
if(get('coachingagentframework').boardKey!=='COAPM')throw new Error('COAPM blanked');
if(get('tonnybox').boardKey!=='TONPM')throw new Error('TONPM blanked');
const blank=[...r.values()].filter(v=>v.boardId&&!v.boardKey);
if(blank.length)throw new Error('blank: '+JSON.stringify(blank));
console.log('OK PJAN + HOLOC, dead boards preserved, 0 blank boardKeys');"
# NOTE: this asserts ticketKey()'s FALLBACK path — plane.ts:179 prefers data.identifier when present.

# G7: an unreachable provider degrades and never blanks. Runs against a COPY — the live
# fleet file is in no git repo. This is why --hermes-registry <path> is a deliverable:
# pjangler exposes --registry/PJ_PROJECT_REGISTRY and HERMES_FLEET_ENV, but nothing for this file.
cd /home/delorenj/code/33GOD/pjangler && cp ~/.hermes/agents-registry.yaml /tmp/pid2-degrade.yaml \
  && PLANE_BASE=https://127.0.0.1:9 node dist/index.js project identity --all --apply --json \
       --hermes-registry /tmp/pid2-degrade.yaml | python3 -c '
import sys,json; d=json.load(sys.stdin)
assert d["degraded"], "expected degraded[]"
e=d["degraded"][0]; assert e.get("workspace") and "connect" in (e.get("reason","")+e.get("error","")).lower(), e
print("OK degraded", len(d["degraded"]))' \
  && python3 -c "
import yaml,os
s=yaml.safe_load(open('/tmp/pid2-degrade.yaml'))['agents']
l=yaml.safe_load(open(os.path.expanduser('~/.hermes/agents-registry.yaml')))['agents']
assert s['holocene-pm']['plane']['identifier']=='HOLOC', 'last-good blanked'
assert l['holocene-pm']['plane']['identifier']=='HOLOC', 'live registry touched'
print('OK last-good preserved, live registry untouched')"

# the writer round-trips without losing fields — the file carries provisioned_at (30/30),
# hermes (30/30), slack (9), telegram.bot_id, and a top-level `gateways` block
cd /home/delorenj/code/33GOD/pjangler && node tests/project-identity-regressions.mjs \
  && python3 -c "import json; t=json.load(open('package.json'))['scripts']['test']; assert 'tests/project-identity-regressions.mjs' in t; print('OK gated by npm test')"
```

---

**PID-3 — Delete pjangler's TS minters; stamp provenance** (S, depends-on: PID-0, PID-2)

`deriveProjectIdentifier` (`index.ts:929`, consumed `:1016`) and `mcp-server.ts:580` are minting sites 3 and 4. Demote the first to `proposeProjectIdentifier` whose output may only ever land as `"proposed"`; delete the second. Thread the adapter's `identifier` through `TicketProviderBoardResult` (`:711`) → the create-board parse (`:843-857`) → `linkTicketProviderBoard` (`:1172`) → `buildTicketProviderBlock` (`:558`).

`touches:` `pjangler/src/project/index.ts`, `pjangler/src/mcp-server.ts`, `pjangler/tests/pjan-30-regressions.mjs`, `pjangler/tests/project-registry-regressions.mjs`, `pjangler/templates/commonproject/` *(submodule: `copier.yml`, `template/.project.json.jinja`)*

```bash
# pjangler's OWN source is clean — no slice-mint, no derive helper
cd /home/delorenj/code/33GOD/pjangler && test -d src \
  && test -z "$(grep -rnE 'slice\(0, ?4\)\.toUpperCase|deriveProjectIdentifier' --include='*.ts' src)" \
  && echo 'OK pjangler/src clean'
# baseline today: 3 hits — index.ts:929 (def), index.ts:1016 (call), mcp-server.ts:580

# the CommonProject template is a SEPARATE git repo (mode 160000, git@github.com:delorenj/CommonProject.git)
cd /home/delorenj/code/33GOD/pjangler/templates/commonproject \
  && grep -q 'identifier_source' template/.project.json.jinja && echo 'OK scaffold declares provenance'
cd /home/delorenj/code/33GOD/pjangler && git submodule status templates/commonproject | grep -qv '^[+-]' \
  && echo 'OK gitlink bumped, so pjangler ships the fixed template'

# provenance is stamped in both directions and the adapter's value wins over the proposal
cd /home/delorenj/code/33GOD/pjangler && python3 -c "
import re; s=open('src/project/index.ts').read()
assert re.search(r'parsed\.identifier', s), 'create_board result must read the provider identifier back'
assert re.search(r'identifier_source\s*[:=]\s*[\"\x27]provider', s), 'provider stamp missing'
assert re.search(r'identifier_source\s*[:=]\s*[\"\x27]proposed', s), 'proposed stamp missing'
assert s.count('identifier_source') >= 4, s.count('identifier_source')
assert 'identifier_source' in s.split('TICKET_PROVIDER_OWNED_KEYS')[1][:300], 'must be an owned key or upsert drops it'
print('OK')"

# the fixture adapter emits an identifier, so the new read-back has something to read
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && python3 -c "s=open('tests/pjan-30-regressions.mjs').read(); assert '\"identifier\"' in s, 'fake create_board must emit identifier'; print('OK fixture')" \
  && node tests/pjan-30-regressions.mjs && node tests/project-registry-regressions.mjs && node tests/mcp-server-regressions.mjs

# a fresh scaffold can never claim provider provenance. `&&` throughout so the assert's
# exit code survives; --no-tui because a bare `pj init` prompts in an interactive terminal.
cd /home/delorenj/code/33GOD/pjangler && rm -rf /tmp/pidsmoke /tmp/pid3-reg.yaml \
  && node dist/index.js init pidsmoke --target-dir /tmp/pidsmoke --registry /tmp/pid3-reg.yaml --apply --no-tui --json >/dev/null \
  && python3 -c '
import json, yaml
m=json.load(open("/tmp/pidsmoke/.project.json"))["ticket_provider"]
assert m.get("identifier_source")=="proposed", m
assert m.get("state")!="linked", m
r=yaml.safe_load(open("/tmp/pid3-reg.yaml"))["projects"]["pidsmoke"]["ticket_provider"]
assert r.get("identifier_source")=="proposed", r
print("OK proposed in manifest and registry")' \
  && rm -rf /tmp/pidsmoke /tmp/pid3-reg.yaml && echo OK

# G2: no CLI path may stamp provider. `pj init --identifier <x>` exists today (src/index.ts:392,619).
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js init x --identifier FOO --no-tui --json 2>&1 \
  | python3 -c 'import sys; s=sys.stdin.read(); assert "\"identifier_source\": \"provider\"" not in s, s; print("OK --identifier cannot claim provider")'
```

---

**PID-4 — Make an unconfirmed link unrepresentable: five validator rules** (M, depends-on: PID-2, PID-3)

Add R1–R5 to the two existing validators and repair the **three** live records they surface. Must land after PID-2/PID-3 have stamped provenance, or `loadProjectRegistry` — which every `pj` command calls at `src/project/index.ts:318` — throws and bricks the CLI.

`touches:` `pjangler/src/project/index.ts`, `pjangler/tests/project-registry-regressions.mjs`, `~/.config/pjangler/projects.yaml`

```bash
# BEFORE — doctor is green on a registry holding a duplicate board_id AND an invalid state
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --json \
  | python3 -c 'import sys,json; print("before ok =", json.load(sys.stdin)["ok"])'
# expect today: before ok = True

# Validator failures surface on STDERR with a non-zero exit — `project doctor --json` emits
# {ok, registryPath, checkedProjects, issues} and has NO `errors` key; on a throw stdout is EMPTY.
# R4 catches the live intelliforia collision; the 22 blank board_ids MUST be exempt.
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null && python3 -c "
import yaml,os
p=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']
assert p['intelliforia']['ticket_provider']['board_id']==p['intelliforia-mobile']['ticket_provider']['board_id']=='687535e9873b89478afef689'
assert len([k for k,v in p.items() if not (v['ticket_provider'].get('board_id') or '').strip()])==22
print('OK collision present; 22 blank board_ids must be exempt')"
set +e; err=$(node dist/index.js project doctor --json 2>&1 >/dev/null); rc=$?
test $rc -ne 0 && printf '%s' "$err" | grep -q 'Duplicate project board_id' && echo 'OK R4 fires' || { echo "FAIL rc=$rc $err"; exit 1; }

# R1 rejects intelliforia's state:"active". Assert the ticket_provider message specifically —
# `state is invalid` alone also matches the pre-existing notebook rule at index.ts:1587.
cd /home/delorenj/code/33GOD/pjangler && python3 -c "
import yaml,copy,tempfile,os
r=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))
x=copy.deepcopy(r['projects']['delonet']); x['slug']='x'; x['ticket_provider']['state']='active'
r['projects']={'x':x}; p=tempfile.mktemp(suffix='.yaml'); yaml.safe_dump(r,open(p,'w')); print(p)" > /tmp/pid4-bad
set +e; err=$(node dist/index.js project doctor --registry "$(cat /tmp/pid4-bad)" --json 2>&1 >/dev/null); rc=$?
test $rc -ne 0 && printf '%s' "$err" | grep -q 'ticket_provider state is invalid' && echo 'OK R1 fires' || { echo "FAIL rc=$rc $err"; exit 1; }

# R3 — all four cases, asserted behaviorally through the CLI
cd /home/delorenj/code/33GOD/pjangler && python3 - <<'PY' && echo 'OK R3 fires'
import yaml, os, copy, subprocess, tempfile
r = yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))
base = copy.deepcopy(r['projects']['delonet'])
B = '49035613-5b42-4a79-88d6-bfaf53e07473'
def run(tp):
    x = copy.deepcopy(base); x['slug'] = 'x'; x['ticket_provider'] = tp
    reg = dict(r); reg['projects'] = {'x': x}
    p = tempfile.mktemp(suffix='.yaml'); yaml.safe_dump(reg, open(p, 'w'))
    return subprocess.run(['node','dist/index.js','project','doctor','--registry',p,'--json'],
                          capture_output=True, text=True).returncode
W = {'type':'plane','workspace':'33god','identifier':'DNET'}
for tp, ok, label in [
    ({**W, 'state':'linked'},                                                   False, 'linked+no board_id'),
    ({**W, 'board_id':B, 'state':'linked', 'identifier_source':'proposed'},      False, 'linked+proposed'),
    ({**W, 'board_id':B, 'state':'linked', 'identifier_source':'provider'},      True,  'linked+provider'),
    ({**W, 'state':'planned', 'identifier_source':'proposed'},                   True,  'planned+proposed'),
]:
    rc = run(tp); assert (rc == 0) == ok, f'{label}: rc={rc}'
    print(('  pass ' if ok else '  reject ') + label)
PY

# R5 — the same identifier in two DIFFERENT workspaces becomes legal.
# Verified live: this exact fixture throws today with "Duplicate project identifier: DUP used by wsa and wsb".
cd /home/delorenj/code/33GOD/pjangler && python3 - <<'PY' > /tmp/pid4-two-ws
import yaml, os, copy, tempfile
r = yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))
b = copy.deepcopy(r['projects']['delonet'])
a = copy.deepcopy(b); a.update(slug='wsa', repo_path='/tmp/wsa')
a['ticket_provider'].update(workspace='33god', identifier='DUP', board_id='11111111-1111-1111-1111-111111111111',
                            state='linked', identifier_source='provider')
c = copy.deepcopy(a); c.update(slug='wsb', repo_path='/tmp/wsb')
c['ticket_provider'] = dict(a['ticket_provider'], workspace='automaticai', board_id='22222222-2222-2222-2222-222222222222')
r['projects'] = {'wsa': a, 'wsb': c}
p = tempfile.mktemp(suffix='.yaml'); yaml.safe_dump(r, open(p, 'w')); print(p)
PY
mkdir -p /tmp/wsa /tmp/wsb && node dist/index.js project doctor --registry "$(cat /tmp/pid4-two-ws)" --json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["ok"], d; print("OK R5 allows cross-workspace DUP")'

# AFTER repairing the THREE offending records, the live registry validates clean.
#   intelliforia         R1 state "active" -> skipped (or linked with its own board) + R4 board_id
#   intelliforia-mobile  R4 own board_id + R3 identifier_source: provider
#   delonet              R3 identifier_source: provider   (DNET already confirmed by PID-2)
# Back up first — this file is in NO git repo and already carries two .bak-* copies.
cp ~/.config/pjangler/projects.yaml "$HOME/.config/pjangler/projects.yaml.pre-pid4"
cd /home/delorenj/code/33GOD/pjangler && python3 -c "
import yaml,os
p=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']
linked=[k for k,v in p.items() if v['ticket_provider'].get('state')=='linked']
for k in linked:
    tp=p[k]['ticket_provider']
    assert tp.get('board_id') and tp.get('identifier') and tp.get('identifier_source')=='provider', (k,tp)
assert not [k for k,v in p.items() if v['ticket_provider'].get('state')=='active'], 'stale state:active remains'
print('OK', len(linked), 'linked records provider-derived')" \
  && node dist/index.js project doctor --json | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["ok"], d; print("OK live registry validates")' \
  && node tests/project-registry-regressions.mjs
```

---

**PID-5 — `pj project adopt`: recover the board_ids already on disk** (M, depends-on: PID-0, PID-4)

22 of 25 records have `board_id: ""`, and **10** of them are already answered by the repo's own `.project.json` (`bb candystore deckard james-brennan mcp-server-trello pjangler project slowburns srvls ssbnk`). The hermes join recovers 8 — a strict subset — so the union is 10, not 21. Write `projects.yaml` only; never a manifest (A1); adopt only repos already registered, not the 47 unbound boards (A4).

`touches:` `pjangler/src/project/adopt.ts` (new), `pjangler/src/index.ts`, `pjangler/tests/project-adopt-regressions.mjs` (new), `pjangler/package.json`, `~/.config/pjangler/projects.yaml`

```bash
# BEFORE — 22 of 25 empty
python3 -c "import yaml,os; p=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']; e=[s for s,v in p.items() if not ((v.get('ticket_provider') or {}).get('board_id') or '').strip()]; print('empty',len(e),'of',len(p))"
# expect today: empty 22 of 25

# fail loudly if the subcommand is absent, before any pipe swallows it as a JSONDecodeError
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null
node dist/index.js project adopt --help >/dev/null 2>&1 || { echo 'FAIL: pj project adopt not registered'; exit 1; }

# dry-run enumerates exactly the 10, from the two declared sources, and writes nothing
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project adopt --from-manifest --from-hermes --json \
  | python3 -c '
import sys,json; d=json.load(sys.stdin); prop=d["proposed"]
got={p["slug"]:p["board_id"] for p in prop}
want={"bb","candystore","deckard","james-brennan","mcp-server-trello","pjangler","project","slowburns","srvls","ssbnk"}
assert set(got)==want, sorted(set(got)^want)
assert all(p["source"] in ("manifest","hermes") for p in prop), prop
assert got["pjangler"]=="18a79832-00fb-4146-b054-d88528f9fef3", got["pjangler"]
assert got["bb"]=="10d06f8d-c110-4ce5-beaa-0914534b090a", got["bb"]
assert all(str(p.get("identifier","") or "").strip() for p in prop), [p["slug"] for p in prop if not str(p.get("identifier","") or "").strip()]
print("OK proposes", len(prop))'
# expect: OK proposes 10.  The identifier assertion matters: pjangler-pm carries plane.identifier:""
# in the hermes registry — adopt must fall back to the manifest or omit the key, never write "".

cd /home/delorenj/code/33GOD/pjangler && cp ~/.config/pjangler/projects.yaml /tmp/pid5-a \
  && node dist/index.js project adopt --from-manifest --from-hermes >/dev/null \
  && diff -q /tmp/pid5-a ~/.config/pjangler/projects.yaml && echo 'OK dry-run inert'

# --apply lands exactly what dry-run promised, and the manifest on disk always wins
cd /home/delorenj/code/33GOD/pjangler && cp ~/.config/pjangler/projects.yaml /tmp/pid5-before.yaml
EXPECT=$(node dist/index.js project adopt --from-manifest --from-hermes --json | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["proposed"]))')
node dist/index.js project adopt --from-manifest --from-hermes --apply >/dev/null
python3 -c "
import yaml,os,json
b=yaml.safe_load(open('/tmp/pid5-before.yaml'))['projects']
a=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']
bid=lambda v: ((v.get('ticket_provider') or {}).get('board_id') or '').strip()
new=[s for s in a if not bid(b[s]) and bid(a[s])]
assert len(new)==int('$EXPECT'), (len(new), '$EXPECT')
for s in new:
    m=json.load(open(os.path.join(os.path.expanduser(a[s]['repo_path']),'.project.json')))
    mb=((m.get('ticket_provider') or {}).get('board_id') or '').strip()
    if mb: assert bid(a[s])==mb, (s, bid(a[s]), mb)
assert len(a)==25, len(a)
print('OK adopted',len(new),'still-empty',sum(1 for v in a.values() if not bid(v)))"
# expect: OK adopted 10 still-empty 12   (the other 12 hold board_id:"" in BOTH sources)

# NOT ONE manifest was rewritten — checked across every registered repo, not a hand-picked three.
# (agentboard's registry repo_path is /home/delorenj/Documents/Codex/.../agentboard and is not a git repo.)
python3 -c "
import yaml,os,subprocess
p=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']
checked=skipped=0
for s,v in p.items():
    r=os.path.expanduser(v['repo_path'])
    if not os.path.isdir(os.path.join(r,'.git')): skipped+=1; print('SKIP not-a-git-repo:',s,r); continue
    if not os.path.exists(os.path.join(r,'.project.json')): continue
    assert subprocess.run(['git','-C',r,'diff','--quiet','--','.project.json']).returncode==0, ('DIRTY',s,r)
    checked+=1
assert checked>=18, ('too few manifests verified',checked)
print('OK no manifest rewrites; checked',checked,'skipped',skipped)"

# adopted records stay `planned` until a provider confirms them — a manifest is not a provider
python3 -c "
import yaml,os
tp=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']['pjangler']['ticket_provider']
assert tp['board_id']=='18a79832-00fb-4146-b054-d88528f9fef3', tp
assert tp.get('identifier_source')=='proposed', tp
assert tp.get('state')!='linked', ('manifest-sourced binding must not claim linked', tp)
print('OK', tp['board_id'][:8], tp['identifier_source'], tp['state'])"

cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --json \
  | python3 -c 'import sys,json; assert json.load(sys.stdin)["ok"]; print("OK doctor")'
cd /home/delorenj/code/33GOD/pjangler && node tests/project-adopt-regressions.mjs \
  && for t in pjan-84-orphan-adoption-regressions pjan-84-registry-flag-regressions registry-cache-parity-regressions registry-root-ladder-regressions project-registry-regressions; do node tests/$t.mjs || exit 1; done \
  && echo 'OK suites green'
```

---

**PID-6 — Model the second workspace; triage the two dead boards** (S, depends-on: PID-4, PID-5)

Half the four-agent premise is already true and half is false. `automatic-ai-pm` (AAI) and `james-brennan-pm` (JIMB) are **already correct** in `automaticai`; `coachingagentframework-pm` (d7f7b5f6) and `tonnybox-pm` (b9016a74) point at boards that 404 in **both** workspaces. Mirror the two real bindings into the SSOT and surface the two dead ones as `unresolved[]` — never relabel them.

`touches:` `pjangler/src/project/identity.ts`, `pjangler/src/project/boardQuery.ts`, `~/.config/pjangler/projects.yaml`

```bash
# No new secret is needed. Verified: the EXISTING PLANE_33GOD_API_KEY returns HTTP 200 on
# workspace automaticai (7 projects: AAI GRIGOR LAMP CAF MARKETJANG JIMB CFO). The key is
# user-scoped, so this is a shared-key fallback, not a credential-provisioning story.
P=/home/delorenj/code/33GOD/bloodbank/agents/hermes/pm/.scripts/providers/plane.sh
grep -q 'PLANE_API_KEY' "$P" || { echo 'FAIL: no shared PLANE_API_KEY fallback'; exit 1; }
env -u PLANE_AUTOMATICAI_API_KEY sh "$P" describe_board automaticai c2cbdc01-bcce-46b9-b10c-7bd620a81cd4 \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["identifier"]=="AAI" and d["workspace"]=="automaticai", d; print("OK", d["identifier"], d["workspace"])'
# expect: OK AAI automaticai   (REQUIRES network + an `op` session)

# identity sweeps BOTH workspaces in one pass; degraded[] is tolerated per G7 and must name its cause
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && node dist/index.js project identity --all --json | python3 -c '
import sys,json; d=json.load(sys.stdin)
ws={(p.get("workspace") or (p.get("ticket_provider") or {}).get("workspace")) for p in d["projects"]}
assert {"33god","automaticai"} <= ws, sorted(w for w in ws if w)
for g in d.get("degraded", []): assert g.get("workspace") and g.get("reason"), g
byb={p.get("board_id"):p for p in d["projects"] if p.get("board_id")}
for bid, ident in (("c2cbdc01-bcce-46b9-b10c-7bd620a81cd4","AAI"), ("a8a12be1-b3ab-44f4-ab24-abe8829aeb72","JIMB")):
    p=byb.get(bid); assert p and p["identifier"]==ident and p["identifier_source"]=="provider", (bid,p)
print("OK workspaces:", sorted(w for w in ws if w))'
# expect: OK workspaces: ['33god', 'automaticai']

# the SSOT carries the second-workspace binding, not just the hermes mirror.
# james-brennan sits in the registry today with board_id:"" and a MINTED identifier "JAME".
python3 -c "
import yaml, os
p=yaml.safe_load(open(os.path.expanduser('~/.config/pjangler/projects.yaml')))['projects']
tp=p['james-brennan']['ticket_provider']
assert tp['workspace']=='automaticai' and tp['identifier']=='JIMB' and tp['identifier_source']=='provider', tp
print('OK SSOT models automaticai')"

# the two DEAD boards are reported, never falsely claimed
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project identity --all --json | python3 -c '
import sys,json; d=json.load(sys.stdin)
un={x["agent_id"] for x in d["unresolved"]}
assert {"coachingagentframework-pm","tonnybox-pm"} <= un, un
print("OK 2 dead boards surfaced as unresolved:", sorted(un))'
python3 -c "
import yaml, os
a=yaml.safe_load(open(os.path.expanduser('~/.hermes/agents-registry.yaml')))['agents']
for k in ('coachingagentframework-pm','tonnybox-pm'):
    pl=a[k]['plane']
    assert (pl.get('identifier') or '').strip(), (k,'blanked')
    assert pl.get('identifier_source')!='provider', (k,'claimed provider identity for a 404 board')
print('OK dead boards keep last-good, claim nothing')"
# Re-linking CAF to its real board (bf80b541) and deciding tonnybox's fate is a SEPARATE ticket.

cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --json \
  | python3 -c 'import sys,json; assert json.load(sys.stdin)["ok"]; print("OK")' && node tests/project-registry-regressions.mjs
```

---

**PID-7 — Self-testing minting gate** (S, depends-on: PID-1, PID-3)

A grep gate that ships with a fixture proving it *fails* when a minting line is reintroduced (G6). Proposing an identifier on a brand-new board is legitimate; **persisting a proposal as truth** is not — the gate targets persistence.

`touches:` `pjangler/.mise/scripts/no-minting.sh` (new), `pjangler/mise.toml`, `pjangler/tests/fixtures/minting-positive.sh.txt` (new), `pjangler/tests/no-minting-regressions.mjs` (new), `pjangler/package.json`, `bloodbank/ops/smoketest/smoketest-no-minting.sh` (new), `bloodbank/mise.toml`

```bash
# clean tree passes. The DEFAULT scan set excludes tests/fixtures — assert that exclusion is
# deliberate, since the positive fixture lives inside the repo the gate scans.
cd /home/delorenj/code/33GOD/pjangler && mise run projects:no-minting; rc=$?; echo "exit=$rc"; test "$rc" -eq 0
cd /home/delorenj/code/33GOD/pjangler && mise run projects:no-minting 2>&1 | grep -q 'excluded: tests/fixtures' \
  && echo 'OK default scan documents its exclusion'

# the negative half: the gate MUST fail on the fixture. mise propagates the literal exit code,
# so assert non-zero, not specifically 1.
cd /home/delorenj/code/33GOD/pjangler && MINTING_SCAN_EXTRA=tests/fixtures mise run projects:no-minting; rc=$?
test "$rc" -ne 0 && echo 'OK gate fails on the positive fixture'
cd /home/delorenj/code/33GOD/pjangler && MINTING_SCAN_EXTRA=tests/fixtures mise run projects:no-minting 2>&1 \
  | grep -q 'minting-positive' && echo 'OK gate names the offending file'

# it runs automatically, not only when a human types it
cd /home/delorenj/code/33GOD/pjangler && node tests/no-minting-regressions.mjs \
  && python3 -c "import json; t=json.load(open('package.json'))['scripts']['test']; assert 'no-minting' in t, t; print('OK gated by npm test')"

# the same gate runs from the repo that holds the DEPLOYED scripts, under bloodbank's own
# smoketest:* convention so it joins the smoketest:ops chain rather than rotting
cd /home/delorenj/code/33GOD/bloodbank && mise run smoketest:no-minting; rc=$?; echo "exit=$rc"; test "$rc" -eq 0
cd /home/delorenj/code/33GOD/bloodbank && grep -q 'smoketest:no-minting' mise.toml \
  && mise run smoketest:ops >/dev/null 2>&1 && echo 'OK wired into smoketest:ops'
```

---

### Optional — durable surface, ship only if wanted

---

**PID-8 — Resolve a project by any of its identities** (M, depends-on: PID-4)

`pj project show bloodbank` fails today (`Project not found in registry: bloodbank`) because the key is `bb`; the 33GOD platform is keyed `project`. Add `resolveProjectRef(slug | alias | board_id | identifier | repo_path)` beside `getProject` (`index.ts:1463`) and `aliases[]`, so no consumer needs the historical key. Ambiguity is an error listing every candidate. Agent-id resolution is **out of scope**: `ProjectRecord.agents` is keyed by role (`delonet → agents.director`), not by fleet agent id.

`touches:` `pjangler/src/project/index.ts`, `pjangler/src/index.ts`, `pjangler/src/mcp-server.ts`, `pjangler/tests/project-registry-regressions.mjs`, `~/.config/pjangler/projects.yaml`

```bash
# back up the un-versioned SSOT first: loadProjectRegistry validates on EVERY load, so one
# colliding alias bricks `pj project list`, `pj init`, and `pj project doctor` at once
cp -n ~/.config/pjangler/projects.yaml "$HOME/.config/pjangler/projects.yaml.pre-pid8" 2>/dev/null; true

# four handles, one record — no `sort -u`, which would let 3 of 4 fail silently
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && for h in delonet DNET 49035613-5b42-4a79-88d6-bfaf53e07473 /home/delorenj/code/delonet-company; do
       node dist/index.js project show "$h" --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["slug"])' || echo "FAILED:$h"
     done | python3 -c 'import sys; v=[l.strip() for l in sys.stdin if l.strip()]; assert len(v)==4, v; assert set(v)=={"delonet"}, v; print("OK all 4 handles resolve to delonet")'
# all four FAIL today except the bare slug, so this is discriminating

# aliases carry the historical keys AND survive a save round-trip
cd /home/delorenj/code/33GOD/pjangler \
  && node dist/index.js project show bloodbank --json | python3 -c 'import sys,json; assert json.load(sys.stdin)["slug"]=="bb"; print("OK bloodbank->bb")' \
  && node dist/index.js project show 33god --json | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["slug"]=="project" and "33god" in (d.get("aliases") or []), d; print("OK 33god->project")' \
  && python3 -c 'import yaml,os; p=yaml.safe_load(open(os.path.expanduser("~/.config/pjangler/projects.yaml")))["projects"]; assert "bloodbank" in (p["bb"].get("aliases") or []); print("OK alias persisted on disk")' \
  && grep -A4 'PROJECT_REGISTRY_OWNED_KEYS' src/project/index.ts | grep -q 'aliases' \
  && echo 'OK aliases is an owned key (survives registry.upsert)'

# path handling matches validateProjectRegistry's own resolve() at index.ts:460
cd /home/delorenj/code/33GOD/bloodbank && node /home/delorenj/code/33GOD/pjangler/dist/index.js project show . --json \
  | python3 -c 'import sys,json; assert json.load(sys.stdin)["slug"]=="bb"; print("OK relative path resolves")'

# a genuinely ambiguous handle errors and names every candidate. board_id
# 687535e9873b89478afef689 is shared by intelliforia and intelliforia-mobile today.
cd /home/delorenj/code/33GOD/pjangler && OUT=$(node dist/index.js project show 687535e9873b89478afef689 --json 2>&1); RC=$?
python3 - "$RC" "$OUT" <<'PY'
import sys
rc, out = int(sys.argv[1]), sys.argv[2]
assert rc != 0, f'ambiguous handle must exit non-zero, got {rc}'
assert 'ambiguous' in out.lower(), out
assert 'intelliforia' in out and 'intelliforia-mobile' in out, f'must name both candidates: {out}'
print('OK ambiguous handle lists both candidates')
PY
cd /home/delorenj/code/33GOD/pjangler && ! node dist/index.js project show no-such-handle-xyz --json >/dev/null 2>&1 \
  && node dist/index.js project show no-such-handle-xyz --json 2>&1 | grep -qi 'not found' && echo 'OK unknown -> not found'

# alias collisions are rejected by the validator, proven end-to-end on throwaway fixtures
cd /home/delorenj/code/33GOD/pjangler && grep -q 'alias collides with slug' tests/project-registry-regressions.mjs \
  && grep -q 'duplicate alias' tests/project-registry-regressions.mjs && node tests/project-registry-regressions.mjs \
  && echo 'OK collision cases exist and pass'
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["ok"], d["issues"]; print("OK doctor clean")'
```

---

**PID-9 — Gate it: identity drift in `pj project doctor`, unbound as a ratchet** (M, depends-on: PID-2, PID-5, PID-6)

Turn the one-off repair into a standing check. Compare identifier and board_id across registry, manifest, and hermes registry, with the provider read served **from PID-2's cache** (`identifier_fetched_at`) so `doctorProjectRegistry` (`index.ts:1469`) stays synchronous and no AC needs the network. `role.yaml` is deliberately **not** a compared store — pjangler has no per-agent enumerator and adding one is its own ticket.

`touches:` `pjangler/src/project/identity.ts`, `pjangler/src/project/index.ts`, `pjangler/src/index.ts`, `pjangler/.pjangler-unbound-highwater` (new, tracked), `pjangler/tests/project-identity-regressions.mjs`

```bash
# doctor names every store it compared. No network, no 1Password: provider values come from
# the cache PID-2 wrote, and each carries its own fetched_at.
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && node dist/index.js project doctor --json > /tmp/pid9-ac1.json \
  && python3 -c '
import json; d=json.load(open("/tmp/pid9-ac1.json"))
assert set(d["identity"]["stores"])=={"provider_cache","registry","manifest","hermes_registry"}, d["identity"]["stores"]
assert d["identity"]["fetched_at"], "cached provider values must carry their age"
print("OK 4 stores compared")'

# a clean tree passes --strict, and the exit code is actually observed (the pipe would hide it)
cd /home/delorenj/code/33GOD/pjangler \
  && cp ~/.config/pjangler/projects.yaml /tmp/pid9-reg.yaml && cp ~/.hermes/agents-registry.yaml /tmp/pid9-h.yaml \
  && node dist/index.js project doctor --strict --json --registry /tmp/pid9-reg.yaml --hermes-registry /tmp/pid9-h.yaml > /tmp/pid9-ac2.json; rc=$?
python3 -c 'import json; d=json.load(open("/tmp/pid9-ac2.json")); assert d["ok"] and not d["identity"]["drift"], d["identity"]["drift"]; print("OK strict clean")' \
  && test "$rc" -eq 0 && echo 'OK strict exited 0'

# reintroduced drift fails --strict — on a COPY, so no live fleet file is mutated and there is
# no restore step to be skipped by a Ctrl-C
cd /home/delorenj/code/33GOD/pjangler && cp ~/.hermes/agents-registry.yaml /tmp/pid9-drift.yaml \
  && python3 -c "
import yaml; p='/tmp/pid9-drift.yaml'; d=yaml.safe_load(open(p))
d['agents']['holocene-pm']['plane']['identifier']='HOLPM'; yaml.safe_dump(d,open(p,'w'))" \
  && ! node dist/index.js project doctor --strict --hermes-registry /tmp/pid9-drift.yaml >/dev/null 2>&1 \
  && node dist/index.js project doctor --json --hermes-registry /tmp/pid9-drift.yaml | python3 -c '
import sys,json; d=json.load(sys.stdin)
assert any(x.get("agent_id")=="holocene-pm" and x["store"]=="hermes_registry" for x in d["identity"]["drift"]), d["identity"]["drift"]
print("OK drift detected AND strict exited non-zero")'

# unbound is a RATCHET, not a gate — and it must be able to trip
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --strict --json > /tmp/pid9-ac4.json; rc=$?
python3 -c '
import json, pathlib
d=json.load(open("/tmp/pid9-ac4.json")); n=len(d["identity"]["unbound"])
hw=int(pathlib.Path("/home/delorenj/code/33GOD/pjangler/.pjangler-unbound-highwater").read_text().strip())
assert d["ok"], "unbound must never fail the gate (G8)"
assert n<=hw, f"ratchet broken: {n} unbound > high-water {hw}"
print("OK unbound",n,"<= high-water",hw)' && test "$rc" -eq 0
# seed the high-water from this story's own first run — 47 unbound in 33god today, measured.
cd /home/delorenj/code/33GOD/pjangler && PJANGLER_UNBOUND_HIGHWATER=0 node dist/index.js project doctor --strict >/dev/null 2>&1
test $? -ne 0 && echo 'OK ratchet trips when exceeded'
# the read-only run must not rewrite its own baseline, or ratchet_ok is true by construction
cd /home/delorenj/code/33GOD/pjangler && git diff --quiet -- .pjangler-unbound-highwater \
  && echo 'OK read-only run did not rewrite the baseline'

# a stale cache surfaces without manufacturing drift
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js project doctor --json | python3 -c '
import sys,json; d=json.load(sys.stdin)
assert "provider" not in {x["store"] for x in d["identity"]["drift"]}, "a stale cache must not manufacture drift"
print("OK stale-cache handling")'

cd /home/delorenj/code/33GOD/pjangler && node tests/project-identity-regressions.mjs \
  && node tests/project-registry-regressions.mjs && node tests/registry-cache-parity-regressions.mjs && echo OK
# tests/pjan-65-regressions.mjs:186 carries a comment asserting doctor "cannot even see" a
# field-value disagreement. This story makes that false — update it here.
```

---

**PID-10 — `~/.hermes/agents-registry.yaml` becomes a reconciled projection** (M, depends-on: PID-2, PID-5)

Expose `pj registry emit hermes --check|--apply` as a **surgical field reconciler over `plane.identifier` only**, iterating the hermes registry's own 30 entries. Do **not** re-render entries from the `rules.ts:2952` template: that template is lossy against the live file — it emits none of `provisioned_at` (30/30), `hermes` (30/30), `slack` (9), `hindsight`, `reporting`, `internal_role_name`, `telegram.bot_id`, `telegram.provisioning_status`, or the top-level `gateways` block.

`touches:` `pjangler/src/registry/emitHermes.ts` (new), `pjangler/src/index.ts`, `pjangler/tests/registry-emit-hermes-regressions.mjs` (new), `pjangler/package.json`

```bash
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && node dist/index.js registry emit hermes --check --json > /tmp/pid10-drift.json \
  && python3 -c '
import json; d=json.load(open("/tmp/pid10-drift.json"))
by={(x["agent_id"],x["field"]):x for x in d["drift"]}
exp={"candybar-pm":"CANDYS","delodocs-pm":"DOCS","heyma-pm":"HEYMA","holocene-pm":"HOLOC",
     "nautilus-trader-pm":"MKTJNG","pjangler-pm":"PJAN","skillex-pm":"SKRILL","ssbnk-pm":"SSBNK",
     "voxxy-pm":"VOXXY","zshyzsh-pm":"ZSHYZH"}
missing=[a for a in exp if (a,"plane.identifier") not in by]
assert not missing, ("missing drift rows", missing)
bad=[(a,by[(a,"plane.identifier")]["expected"],v) for a,v in exp.items() if by[(a,"plane.identifier")]["expected"]!=v]
assert not bad, bad
deg={x["agent_id"] for x in d["degraded"]}
assert {"coachingagentframework-pm","tonnybox-pm"} <= deg, ("dead boards must degrade, not blank", deg)
print("OK 10 drift rows, 2 degraded")'

# apply writes atomically, leaves no .bak-* (7 already rot beside the file) and no temp residue
BEFORE=$(ls ~/.hermes/agents-registry.yaml.bak-* 2>/dev/null | wc -l); MT_BEFORE=$(stat -c %Y ~/.hermes/agents-registry.yaml)
cd /home/delorenj/code/33GOD/pjangler && cp ~/.hermes/agents-registry.yaml /tmp/pid10-pre.yaml \
  && node dist/index.js registry emit hermes --apply >/dev/null || { echo 'FAIL: apply exited non-zero'; exit 1; }
AFTER=$(ls ~/.hermes/agents-registry.yaml.bak-* 2>/dev/null | wc -l); MT_AFTER=$(stat -c %Y ~/.hermes/agents-registry.yaml)
STRAY=$(ls ~/.hermes/agents-registry.yaml.tmp* ~/.hermes/.agents-registry* 2>/dev/null | wc -l)
[ "$BEFORE" = "$AFTER" ] && [ "$STRAY" = 0 ] && [ "$MT_BEFORE" != "$MT_AFTER" ] \
  && echo 'OK written atomically, no .bak, no residue'

# ONLY plane.identifier changed — nothing else in the 29KB file was touched
python3 -c "
import yaml
pre=yaml.safe_load(open('/tmp/pid10-pre.yaml')); post=yaml.safe_load(open('/home/delorenj/.hermes/agents-registry.yaml'))
assert set(pre)==set(post) and pre.get('gateways')==post.get('gateways') and pre['schema_version']==post['schema_version']
a,b=pre['agents'],post['agents']; assert set(a)==set(b), set(a)^set(b)
for k in a:
    x,y=dict(a[k]),dict(b[k])
    px,py=dict(x.pop('plane',{}) or {}),dict(y.pop('plane',{}) or {})
    assert x==y, ('non-plane fields mutated for %s'%k, {f for f in set(x)|set(y) if x.get(f)!=y.get(f)})
    px.pop('identifier',None); py.pop('identifier',None)
    assert px==py, ('plane fields other than identifier mutated for %s'%k)
print('OK', len(b), 'agents; only plane.identifier changed; provisioned_at/hermes/slack/telegram/gateways intact')"

# idempotent, and --check fails the shell when it is not
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js registry emit hermes --check --json > /tmp/pid10-re.json \
  && python3 -c 'import json; d=json.load(open("/tmp/pid10-re.json")); assert d["drift"]==[], d["drift"]; print("OK idempotent")'

cd /home/delorenj/code/33GOD/pjangler && node tests/registry-emit-hermes-regressions.mjs \
  && python3 -c "import json; t=json.load(open('package.json'))['scripts']['test']; assert 'registry-emit-hermes' in t; print('OK gated')"
# rules.ts:551 `if (current.includes(agentId)) return null` stays as-is — parity remains
# insert-only, `emit hermes` is the documented repair writer, PID-9 catches re-drift.
```

---

**PID-11 — n8n reads a generated route table** (M, depends-on: PID-5, PID-10)

`planeRoutesFromRegistry` (`src/plane.ts:66-83`) keys on `plane.project_id` and takes `boardKey` from `plane.identifier`; its `slug` at `:78` is `firstText(entry.slug) ?? repo`, and hermes entries have **no** `slug` key, so slug always silently degrades to repo. Emit a first-class route table and prefer it, keeping the registry read as the documented fallback.

`touches:` `pjangler/src/registry/emitPlaneRoutes.ts` (new), `pjangler/src/index.ts`, `bloodbank/integrations/n8n-nodes-bloodbank/src/plane.ts`, `.../src/nodes/PlaneBloodbank/PlaneBloodbank.node.ts`, `.../test/plane-normalization.test.cjs`, `.../test/fixtures/plane-routes.sample.json` (new)

```bash
# the emitter emits ONLY linked records — a planned record has no board
cd /home/delorenj/code/33GOD/pjangler && npm run build >/dev/null \
  && node dist/index.js registry emit plane-routes --out "$PWD/.tmp-routes.json" \
  && python3 -c "
import json; d=json.load(open('.tmp-routes.json')); r=d['routes']
assert r, 'no routes emitted'
bad=[x for x in r if not (x.get('board_id') and x.get('identifier') and x.get('slug'))]; assert not bad, bad
assert all(x.get('state')=='linked' for x in r), 'emitter must emit only linked records'
p=[x for x in r if x['slug']=='pjangler'][0]
assert p['identifier']=='PJAN' and p['board_id']=='18a79832-00fb-4146-b054-d88528f9fef3', p
print('OK', len(r), 'routes; pjangler ->', p['identifier'])" \
  && rm -f "$PWD/.tmp-routes.json"

# the node tests run the TS source through the LOCAL tsx (tsx is not on PATH); npm run build is not needed
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && ./node_modules/.bin/tsx --test test/plane-normalization.test.cjs
# new cases, all against test/fixtures/plane-routes.sample.json produced verbatim by the emitter:
#   route-table keys == {board_id, slug, identifier, workspace, state}
#   pjangler payload, sequence_id 12 -> ticket_key 'PJAN-12'   (today: null)
#   holocene payload, sequence_id 12 -> 'HOLOC-12'             (today: 'HOLPM-12')
#   route.slug comes from the table, never the repo fallback
#   resolveRouteTablePath(undefined) === resolveRouteTablePath('') === DEFAULT_ROUTE_TABLE_PATH

# the repo fallback is gone from the route-table path and RETAINED in the legacy path
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && python3 -c "
import re; s=open('src/plane.ts').read()
new=re.search(r'export function planeRoutesFromRouteTable\(.*?\n\}', s, re.S); assert new, 'planeRoutesFromRouteTable not found'
assert '?? repo' not in new.group(0), 'route-table path must take slug from the table'
old=re.search(r'export function planeRoutesFromRegistry\(.*?\n\}', s, re.S)
assert old and 'firstText(entry.slug) ?? repo' in old.group(0), 'legacy fallback retained deliberately'
print('OK')"

# saved workflows that predate the new param still resolve it (explicit 3rd-arg default)
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && python3 -c "
import re; s=open('src/nodes/PlaneBloodbank/PlaneBloodbank.node.ts').read()
assert re.search(r\"getNodeParameter\(\s*'routeTableFile'\s*,\s*0\s*,\", s, re.S), 'must pass a 3rd-arg default'
assert s.count('agents-registry.yaml')==1, 'registry retained exactly once, as the documented fallback'
assert s.index(\"name: 'routeTableFile'\") < s.index(\"name: 'registryFile'\"), 'route table must read as primary'
print('OK saved-workflow safe, route table primary')"

# --- MUTATING: rsyncs into ~/.n8n/nodes/... and pm2-restarts live n8n ---
cd /home/delorenj/code/33GOD/pjangler && node dist/index.js registry emit plane-routes --out "$HOME/.config/pjangler/plane-routes.json"
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && set -o pipefail && npm run deploy 2>&1 | tail -3 \
  && node -e "
const {planeRoutesFromRouteTable}=require(process.env.HOME+'/.n8n/nodes/node_modules/n8n-nodes-bloodbank/dist/plane.js');
const m=planeRoutesFromRouteTable(JSON.parse(require('fs').readFileSync(process.env.HOME+'/.config/pjangler/plane-routes.json','utf8')));
if(m.size<1)throw new Error('deployed build loaded 0 routes');
const pj=[...m.values()].find(r=>r.slug==='pjangler'); if(!pj||pj.boardKey!=='PJAN')throw new Error(JSON.stringify(pj));
console.log('OK deployed build:',m.size,'routes, pjangler ->',pj.boardKey);"
```

## First slice

**PID-0, then PID-1 — and PID-1 is a forward-port, not an authoring job.**

The read-back everyone assumed still needed writing is already committed in `pjangler/templates/hermes-agent/template/.scripts/providers/plane.sh:171-176,278-289`. Bloodbank's deployed copy is 15 lines behind on `providers/plane.sh` and 83 lines behind on `42-ticket-provider.sh`, which is precisely why nobody noticed. The day PID-1 lands:

- `providers/plane.sh create_board "Holocene" "HOLPM" "probe"` stops returning `{board_id, board_url}` and starts returning `{board_id, board_url, identifier: "HOLOC"}` — reproducible right now as a `KeyError: 'identifier'`.
- `describe_board <workspace> <board_id>` exists as a read-only op, taking workspace as an explicit argument so `.project.json` precedence (`plane.sh:107`, where `PLANE_WORKSPACE` is third in line and structurally unreachable) cannot silently query the wrong workspace.
- The `IDENT="${RAW:0:5}"` arithmetic disappears from all eight identical copies of `40-plane.sh` on disk, and `yaml_set plane.identifier "$IDENT"` at `:49` disappears with it. The next PM provisioned writes what Plane said, so `80-registry.sh:40` and `rules.ts:553` — which both read `role.yaml`'s `plane.identifier` — propagate truth without either being touched (A6).
- The board-lookup branch stops searching by the invented identifier, so a board that already exists as `HOLOC` is no longer invisible to a search for `HOLPM`, and the duplicate-board failure mode is closed at the source.

PID-2 is what the user sees in production, and it needs no deploy: `PlaneBloodbank.node.ts:195` re-reads `~/.hermes/agents-registry.yaml` on every execution with no cache. Paste this before and after:

```bash
cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-nodes-bloodbank && node -e "const{planeRoutesFromRegistry}=require('./dist/plane.js');const yaml=require('yaml'),fs=require('fs'),os=require('os');const r=planeRoutesFromRegistry(yaml.parse(fs.readFileSync(os.homedir()+'/.hermes/agents-registry.yaml','utf8')));console.log([...r.values()].filter(v=>v.boardId&&!v.boardKey).length,'blank boardKeys')"
```

`1 blank boardKeys` today. `0` when PID-2 is done, with ten identifiers corrected on the way.

## Risks & non-goals

**Risks**

- **`npm run build` currently ships a broken CLI.** `src/commands/hermes/EnsureTemplateConfig.ts:1` imports only `homedir` from `node:os` while `:115` calls `platform()`; `:50` uses `realpathSync` and `:450` `describePathType`, neither imported. esbuild bundles with `--packages=external` and never typechecks, so `npx tsc --noEmit` reports 3× TS2304 while the committed `dist/` (which predates the edit) still passes `tests/project-registry-regressions.mjs`. Rebuild and that suite dies with `platform is not defined` — on `pj init --provision-agent`, the exact ingress this epic designates as SSOT. **PID-0 exists solely for this and must land first.** The pjangler worktree is also dirty and on `feat/PJAN-87-board-read-subcommands`, not `main`.
- **`~/.config/pjangler/projects.yaml` is not in a git repo** (`fatal: not a git repository`). No history, no pre-commit guard, no `git unpushed` visibility, and it already carries two `.bak-*` files. Every `--apply` writes through the atomic `saveProjectRegistry` (`index.ts:400-444`) and re-validates before rename; take a `cp` snapshot before each one and do **not** add more `.bak-*` files beside it. The durable fix is two minutes and outside this epic: `git init ~/.config/pjangler && printf '*.bak-*\nconfig.toml\n' > ~/.config/pjangler/.gitignore && git -C ~/.config/pjangler add projects.yaml && git -C ~/.config/pjangler commit -m baseline`.
- **`~/.hermes/agents-registry.yaml` has 7 `.bak-*` copies rotting beside it** and n8n reads it live on every webhook. A malformed write is an immediate outage. Every writer must round-trip through the `yaml` Document API, verify the agent count is unchanged, and rename atomically — a full round-trip was verified to preserve all 30 agents, 9 `slack` blocks, 30 `provisioned_at` fields, and the top-level `gateways` block. Never `safe_dump` it: that sorts keys and drops formatting.
- **PID-4 can brick every `pj` command if it lands early.** `validateProjectRegistry` throws from `loadProjectRegistry` (`index.ts:318`), which `project list`, `project show`, `init`, and `doctor` all call. The dependency order PID-2 → PID-3 → PID-4 is hard, and the three offending records are repaired **inside** PID-4, not deferred.
- **A second global-uniqueness site survives this epic.** `migrations/1783967674032_pjangler-registry.cjs:59-61` creates `CREATE UNIQUE INDEX ux_project_ticket_boards_identifier ON project_ticket_boards (upper(identifier))` with no provider or workspace in the key. After R5 the YAML path accepts a second workspace and the PG path still rejects it. `DualWriteRegistryStore` (`RegistryStore.ts:457`) is best-effort, so this cannot fail a YAML write — but the fix is a follow-up ticket, not this epic (A8).
- **Plane may reject a proposed identifier on create rather than normalize it.** With the read-back in place, `create_board` hard-fails where it previously succeeded with a fiction. That is intended, but it changes the failure mode of `pj init --live`; the error must name the collision and the workspace.
- **A board rename changes its identifier silently.** `identifier_fetched_at` records staleness and `pj project identity` re-reads it, but nothing pushes. A cache with a visible age is strictly better than today's permanent fiction.
- **`rules.ts:551` `upsertRegistryEntry` is insert-only** (`if (current.includes(agentId)) return null`), so `pj parity` can never repair a drifted identifier — only add one. Anyone treating parity as the repair path will conclude the epic did not work. `pj project identity --apply` is the documented repair writer.
- **Several repos' own `.project.json` already hold minted identifiers** — `voxxy VOXPM`, `drumjangler DRUMJ`, `agentboard ABRD`. PID-5 reads manifests as an adoption source for `board_id` **only**; identifiers come exclusively from PID-2's provider read-back, or the fiction re-enters marked as adopted.

**Non-goals — deliberately not done**

- **No bloodbank-owned lockfile, and no second registry.** PJangler's `projects.yaml` is the SSOT; `pj init` is the ingress. Rejected by the user; not revisited.
- **No rewrite of the 137+ `.project.json` files** across ~50 independent repos (A1). They stay authoritative-on-read for the board binding, exactly as `src/describe/index.ts:389-391` documents. PID-5 reads 10 and writes none.
- **No event-sourced JSONL log or fold engine** over registry mutations (A2). Git gives history and revert; the risks section names the one command that puts the registry under git.
- **No synthetic `project_id`** — ULID, uuid5, or otherwise (A3). Plane's UUID is what `planeRoutesFromRegistry` keys on at `plane.ts:69-76` and what `orderingKey`/`dedupeKey` derive from.
- **No adoption of the 47 unbound Plane boards** or the orphan repos under `~/code` (A4). PID-9 reports them as a monotonically-decreasing ratchet and stops there. Nothing here inflates 25 records toward ~190 for someone with "20-some projects."
- **No 32-positional-argv surgery on `80-registry.sh`** (A6). PID-1 fixes the value in `role.yaml`, so it writes the truth untouched. PID-10 makes the emitter authoritative and `--check` the detector; deleting the legacy writer is a follow-up.
- **No Holocene page, no Candystore replay, no new HTTP API, no MCP tool** (A7). The only consumer that broke reads a YAML file; `pj project doctor --json` is the entire read surface.
- **No pjangler PG migrations run** (A8). The two pending migrations and the missing `slug` column stay pending; `DualWriteRegistryStore` remains YAML-authoritative.
- **No change to `ticket_key`'s downstream contract.** It has zero consumers today — display only, since `orderingKey` and `dedupeKey` both use `ticket_id` — which is precisely why correcting its derivation is safe to do first.
- **No re-linking of the two dead boards.** `coachingagentframework` (`d7f7b5f6`) and `tonnybox` (`b9016a74`) 404 in both workspaces. PID-6 surfaces them as `unresolved[]`; pointing CAF at its real board (`bf80b541`) and deciding tonnybox's fate is a separate ticket.
- **No `role.yaml` as a compared store in the doctor gate.** pjangler has no per-agent `role.yaml` enumerator, and `rules.ts` is 7230 lines with per-agent context rather than a reusable projection. Deferred.
- **No agent-id resolution in `pj project show`.** `ProjectRecord.agents` is keyed by role, not by fleet agent id — there is no field to resolve against without synthesizing one.
- **No `yq` in any acceptance criterion.** It is not installed. Every AC uses `python3` + PyYAML 6.0.2, `node`, or `curl`; `jq` is present at `/usr/bin/jq` but unused.