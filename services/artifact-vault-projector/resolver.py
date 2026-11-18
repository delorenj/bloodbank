"""resolver: find the vault folder for a project slug the projector cannot place.

A slug lands here when its artifacts are held: either it has no mapping
(`unmapped`), or its mapped folder vanished (`missing-target`). Projects get
renamed (pile-of-dumb-things -> tower-of-dumb-things -> Tower of Lost
Things), nested (tolt/lost-things-forge), and vault folders get moved, so the
right home is usually findable. Two layers:

1. Evidence (deterministic, local, free). Every vault project folder is
   scored against the slug's aliases: the slug, the registry name, the repo
   basename, `renamed_from` ids in both directions, parent and child
   registry projects (nested repo paths), the folders other slugs already
   map to, acronyms (tolt == TowerOfLostThings), file names inside the
   folder ("Tower of Dumb THings.md"), and for a missing target, a folder of
   the same name elsewhere in the vault that still holds date folders.
2. Decision (Jev, `typesafe/jev`, the fleet's decision model). Jev picks one
   of the scored candidates, "create a new folder" (registered projects
   only) or "hold". Without a Jev key or on a Jev failure, only an
   unambiguous deterministic match is taken; everything else stays held.

The resolver decides; it never writes. main.py applies a decision (mapping
file, optional mkdir, re-projection of the held envelopes) and records it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SKIP_DIRS = {".trash", ".obsidian", ".git", ".stversions", ".stfolder", "agents", "node_modules"}
DEFAULT_JEV_URL = "https://jev.delo.sh/typesafe/v1/systemone"
DEFAULT_REGISTRY_URL = "http://127.0.0.1:8764/v1/registry"
MAX_CANDIDATES = 10
STRONG = 0.6          # a candidate this strong suppresses the "create" option (Infra vs Infrastructure)
HOLD = "hold"
CREATE_PREFIX = "create:"


# ── name handling ──────────────────────────────────────────────────────────

def tokens(text: str) -> list[str]:
    """Lowercase word tokens; camelCase, kebab, snake and spaces all split."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t]


def joined(text: str) -> str:
    return "".join(tokens(text))


def acronym(text: str) -> str:
    t = tokens(text)
    return "".join(x[0] for x in t) if len(t) >= 2 else ""


def safe_dir_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip(" .")
    return cleaned or "project"


# ── inputs ─────────────────────────────────────────────────────────────────

@dataclass
class Subject:
    slug: str
    hold_kind: str                      # unmapped | missing-target
    current_target: str | None          # the mapping value, when missing-target
    registry: dict[str, Any] | None
    aliases: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)       # registry slugs, nearest first
    children: list[str] = field(default_factory=list)
    renamed_to: list[str] = field(default_factory=list)    # registry slugs that were once this slug
    renamed_from: list[str] = field(default_factory=list)  # ids this slug used to have
    mentioned_by: list[str] = field(default_factory=list)  # registry slugs whose git history names this slug


@dataclass
class Candidate:
    target: str                         # vault-relative dir, or "create:<dir>"
    score: float
    reasons: list[str]


@dataclass
class Decision:
    slug: str
    hold_kind: str
    action: str                         # map | create | hold
    target: str | None
    confidence: float | None
    decider: str                        # jev | deterministic | none
    reason: str
    candidates: list[dict[str, Any]]
    fingerprint: str
    jev: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def fetch_registry(url: str, timeout: float = 10) -> dict[str, dict[str, Any]] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001 - registry down => no registry evidence
        return None
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else None


def _repo(entry: dict[str, Any] | None) -> Path | None:
    value = (entry or {}).get("repo_path")
    return Path(value) if isinstance(value, str) and value else None


def build_subject(
    slug: str,
    hold_kind: str,
    mapping: dict[str, str],
    registry: dict[str, dict[str, Any]] | None,
    *,
    git_search: bool = True,
) -> Subject:
    reg = registry or {}
    entry = reg.get(slug)
    subject = Subject(slug=slug, hold_kind=hold_kind, current_target=mapping.get(slug), registry=entry)
    aliases = {slug}
    if entry:
        if entry.get("name"):
            aliases.add(str(entry["name"]))
        repo = _repo(entry)
        if repo:
            aliases.add(repo.name)
        rf = entry.get("renamed_from")
        for item in rf if isinstance(rf, list) else [rf]:
            if isinstance(item, dict) and item.get("project_id"):
                subject.renamed_from.append(str(item["project_id"]))
                aliases.add(str(item["project_id"]))
        if repo:
            nearest: list[tuple[int, str]] = []
            for other_slug, other in reg.items():
                if other_slug == slug:
                    continue
                other_repo = _repo(other)
                if not other_repo:
                    continue
                if other_repo != repo and other_repo in repo.parents:
                    nearest.append((len(other_repo.parts), other_slug))
                elif repo != other_repo and repo in other_repo.parents:
                    subject.children.append(other_slug)
            subject.parents = [s for _, s in sorted(nearest, reverse=True)]
    # A slug that a registered project used to carry (the old id of a rename).
    for other_slug, other in reg.items():
        rf = other.get("renamed_from")
        for item in rf if isinstance(rf, list) else [rf]:
            if isinstance(item, dict) and item.get("project_id") == slug:
                subject.renamed_to.append(other_slug)
    if entry is None and not subject.renamed_to and git_search:
        subject.mentioned_by = git_mentions(slug, registry)
    if subject.current_target:
        aliases.add(Path(subject.current_target).name)
    subject.aliases = sorted(aliases)
    return subject


def git_mentions(slug: str, registry: dict[str, dict[str, Any]] | None, timeout: float = 5) -> list[str]:
    """Registered projects whose git history names an unregistered slug.

    The rename trail the registry did not record: tower-of-dumb-things is no
    registry slug, but tolt's history says "pile-of-dumb-things-pm retired".
    Only for slugs of 5+ chars, and only the slug itself (fixed string).
    """
    if len(slug) < 5:
        return []
    # Whole-token match ("infra" must not hit "infrastructure"), ERE-escaped.
    pattern = f"(^|[^a-z0-9-]){re.escape(slug)}([^a-z0-9-]|$)"
    hits: list[str] = []
    seen: set[Path] = set()
    for other_slug, entry in sorted((registry or {}).items()):
        repo = _repo(entry)
        if not repo or repo in seen or not (repo / ".git").exists():
            continue
        seen.add(repo)
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", "-i", "-E", f"--grep={pattern}", "-n", "1", "--format=%h"],
                capture_output=True, text=True, timeout=timeout, check=False,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 - a slow or broken repo is no evidence
            continue
        if out:
            hits.append(other_slug)
    # A slug named across many repos is a common word, not a rename trail.
    return hits if len(hits) <= 2 else []


# ── evidence ───────────────────────────────────────────────────────────────

def _alias_set(subject: Subject, registry: dict[str, dict[str, Any]] | None, slugs: list[str]) -> set[str]:
    out: set[str] = set()
    for s in slugs:
        out.add(joined(s))
        entry = (registry or {}).get(s) or {}
        if entry.get("name"):
            out.add(joined(str(entry["name"])))
        repo = _repo(entry)
        if repo:
            out.add(joined(repo.name))
    return {a for a in out if a}


def _has_date_child(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            return any(e.is_dir(follow_symlinks=False) and DATE_DIR_RE.match(e.name) for e in it)
    except OSError:
        return False


def _child_names(path: Path, depth: int = 2) -> list[str]:
    names: list[str] = []
    stack = [(path, 0)]
    while stack:
        cur, d = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if e.name.startswith(".") or DATE_DIR_RE.match(e.name):
                        continue
                    names.append(e.name)
                    if d + 1 < depth and e.is_dir(follow_symlinks=False):
                        stack.append((Path(e.path), d + 1))
        except OSError:
            continue
        if len(names) > 4000:
            break
    return names


def vault_folders(vault_root: Path, projects_dir: str, mapping: dict[str, str]) -> list[str]:
    """Vault-relative candidate folders: Projects/* plus every mapped target."""
    out: set[str] = set()
    base = vault_root / projects_dir
    try:
        with os.scandir(base) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "_")):
                    out.add(f"{projects_dir}/{e.name}")
    except OSError:
        pass
    for target in mapping.values():
        p = vault_root / target
        if p.is_dir() and not p.is_symlink():
            out.add(target)
    return sorted(out)


def find_moved(vault_root: Path, basename: str, max_depth: int = 4) -> list[str]:
    """Folders named like a vanished target that still hold date folders."""
    want = basename.casefold()
    hits: list[str] = []
    stack = [(vault_root, 0)]
    while stack:
        cur, d = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False) or e.name in SKIP_DIRS or e.name.startswith("."):
                        continue
                    if DATE_DIR_RE.match(e.name):
                        continue
                    p = Path(e.path)
                    if e.name.casefold() == want and _has_date_child(p):
                        hits.append(str(p.relative_to(vault_root)))
                    if d + 1 < max_depth:
                        stack.append((p, d + 1))
        except OSError:
            continue
    return sorted(hits)


def score_candidates(
    subject: Subject,
    vault_root: Path,
    projects_dir: str,
    mapping: dict[str, str],
    registry: dict[str, dict[str, Any]] | None,
) -> list[Candidate]:
    own = _alias_set(subject, registry, [subject.slug]) | {joined(a) for a in subject.aliases}
    own = {a for a in own if a}
    child_aliases = _alias_set(subject, registry, subject.children)
    mention_aliases = _alias_set(subject, registry, subject.mentioned_by)
    by_target: dict[str, str] = {}
    for s, t in mapping.items():
        by_target.setdefault(t, s)
    related_mapped: dict[str, str] = {}
    for rel_slug, why in (
        [(s, "renamed project") for s in subject.renamed_to]
        + [(s, "former id") for s in subject.renamed_from]
        + [(s, "parent project") for s in subject.parents[:1]]
        + [(s, "sub-project") for s in subject.children]
        + [(s, "project whose git history names this slug") for s in subject.mentioned_by]
    ):
        if rel_slug in mapping:
            related_mapped.setdefault(mapping[rel_slug], f"already the folder of {why} {rel_slug!r}")

    scores: dict[str, Candidate] = {}

    def bump(target: str, score: float, reason: str) -> None:
        c = scores.setdefault(target, Candidate(target=target, score=0.0, reasons=[]))
        c.score = max(c.score, score)
        if reason not in c.reasons:
            c.reasons.append(reason)

    for folder in vault_folders(vault_root, projects_dir, mapping):
        if subject.current_target and folder == subject.current_target:
            continue
        name = Path(folder).name
        j, acr, toks = joined(name), acronym(name), set(tokens(name))
        for a in own:
            if j == a:
                bump(folder, 1.0, f"folder name matches {a!r}")
            elif len(acr) >= 3 and acr == a:
                bump(folder, 0.9, f"folder acronym {acr!r} matches the slug")
            elif len(a) >= 5 and j.startswith(a):
                # "Infra" -> "Infrastructure" extends the word itself (0.6).
                # "DeLoDocs" -> "DeLoDocs-scout-integration" ends on a word
                # boundary and adds words: a sub-topic folder, a hint only.
                tl, acc, boundary = tokens(name), "", False
                for t in tl[:-1]:
                    acc += t
                    if acc == a:
                        boundary = True
                        break
                if boundary:
                    bump(folder, 0.5, f"folder is a sub-topic of {a!r}")
                else:
                    bump(folder, 0.6, f"folder name starts with {a!r}")
            elif len(a) >= 5 and (a in j or (len(j) >= 5 and j in a)):
                bump(folder, 0.45, f"folder name overlaps {a!r}")
        for alias in subject.aliases:
            at = set(tokens(alias))
            if len(at) >= 2 and toks:
                jac = len(at & toks) / len(at | toks)
                if jac >= 0.5:
                    # Capped below the agreement bar: shared fragments
                    # ("DeLoDocs" vs "DeLoDocs-scout-integration") are a hint,
                    # never enough to file on their own.
                    bump(folder, min(0.55, round(0.4 + 0.4 * jac, 2)), f"shares words with {alias!r}")
        for a in child_aliases:
            if j == a:
                bump(folder, 0.8, f"folder name matches sub-project {a!r}")
        for a in mention_aliases:
            if j == a or (len(acr) >= 3 and acr == a):
                bump(folder, 0.8, f"folder of {a!r}, whose git history names this slug")
        if folder in related_mapped:
            bump(folder, 0.85, related_mapped[folder])
        if folder in by_target and folder not in related_mapped:
            bump(folder, 0.0, f"already mapped for {by_target[folder]!r}")
        long_aliases = {a for a in own if len(a) >= 6}
        if long_aliases:
            for child in _child_names(vault_root / folder):
                cj = joined(Path(child).stem)
                if cj in long_aliases:
                    # A note titled exactly like the project ("Tower of Dumb THings.md").
                    bump(folder, 0.75, f"contains {child!r}, titled like the project")
                    break
                hit = next((a for a in long_aliases if a in cj), None)
                if hit:
                    bump(folder, 0.4, f"contains {child!r} (mentions {hit!r})")

    if subject.hold_kind == "missing-target" and subject.current_target:
        for moved in find_moved(vault_root, Path(subject.current_target).name):
            if moved != subject.current_target:
                bump(moved, 0.95, f"{Path(subject.current_target).name!r} with date folders now lives here (moved?)")

    ranked = sorted((c for c in scores.values() if c.score > 0), key=lambda c: (-c.score, c.target))
    return ranked[:MAX_CANDIDATES]


def create_option(subject: Subject, projects_dir: str, vault_root: Path) -> str | None:
    """`create:<dir>` for a registered project with no existing home."""
    if not subject.registry:
        return None
    name = safe_dir_name(str(subject.registry.get("name") or subject.slug))
    target = f"{projects_dir}/{name}"
    try:
        existing = {e.name.casefold() for e in os.scandir(vault_root / projects_dir)}
    except OSError:
        existing = set()
    if name.casefold() in existing:
        return None  # it exists (any case): that folder is a candidate, not a creation
    return CREATE_PREFIX + target


def fingerprint(subject: Subject, candidates: list[Candidate], create: str | None, mapping: dict[str, str]) -> str:
    blob = json.dumps(
        {
            "slug": subject.slug,
            "kind": subject.hold_kind,
            "registry": {k: (subject.registry or {}).get(k) for k in ("name", "repo_path", "renamed_from", "status")},
            "rel": [subject.parents, subject.children, subject.renamed_to],
            "candidates": [(c.target, c.score) for c in candidates],
            "create": create,
            "mapping": sorted(mapping.items()),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ── Jev ────────────────────────────────────────────────────────────────────

_KEY_CACHE: dict[str, str | None] = {}
_KEY_FAILED_AT: dict[str, float] = {}


def jev_key() -> str | None:
    """JEV_API_KEY from the env, else the fleet's jev-secrets chain (op:// refs)."""
    if os.environ.get("JEV_API_KEY"):
        return os.environ["JEV_API_KEY"]
    if _KEY_CACHE.get("k"):
        return _KEY_CACHE["k"]
    failed_at = _KEY_FAILED_AT.get("t")
    if failed_at is not None and time.monotonic() - failed_at < 300:
        return None  # a failed lookup is retried after 5 minutes, not cached forever
    tool = os.environ.get("ARTIFACT_VAULT_JEV_SECRETS") or next(
        (str(p) for p in (Path.home() / ".local/bin/jev-secrets",
                          Path.home() / ".agents/tools/jev-clairic/bin/jev-secrets") if p.exists()),
        "jev-secrets",
    )
    key = None
    try:
        out = subprocess.run(
            [tool, "export", "JEV_API_KEY"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
        for line in out.splitlines():
            parts = shlex.split(line)
            if len(parts) == 2 and parts[0] == "export" and parts[1].startswith("JEV_API_KEY="):
                key = parts[1].split("=", 1)[1] or None
    except Exception:  # noqa: BLE001 - no key => deterministic fallback
        key = None
    if key:
        _KEY_CACHE["k"] = key
        _KEY_FAILED_AT.pop("t", None)
    else:
        _KEY_FAILED_AT["t"] = time.monotonic()
    return key


def _post_json(url: str, body: dict, key: str, timeout: float) -> dict:
    calls_dir = Path.home() / ".agents/tools/jev-clairic/bin"
    if str(calls_dir) not in sys.path and (calls_dir / "jev_clairic_calls.py").exists():
        sys.path.append(str(calls_dir))
    headers = {"Authorization": "Bearer " + key}
    try:
        from jev_clairic_calls import post_json  # spend log the exporter reads

        return post_json(url, body, headers, consumer="artifact-vault-projector",
                         tool="vault_target", timeout=timeout)
    except ImportError:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)


def describe(subject: Subject, registry: dict[str, dict[str, Any]] | None) -> str:
    lines = [f"Project slug {subject.slug!r} produced artifacts that must be filed in the "
             f"operator's Obsidian vault, one folder per project."]
    if subject.hold_kind == "missing-target":
        lines.append(f"It used to map to {subject.current_target!r}, which no longer exists.")
    else:
        lines.append("It has no vault folder mapping yet.")
    e = subject.registry
    if e:
        lines.append(f"Registry: name {e.get('name')!r}, repo {e.get('repo_path')!r}, status {e.get('status')!r}.")
        if e.get("description"):
            lines.append(f"Description: {str(e['description'])[:300]}")
    else:
        lines.append("The slug is NOT in the project registry (possibly an old name of a renamed project).")
    reg = registry or {}

    def label(s: str) -> str:
        return f"{s} ({(reg.get(s) or {}).get('name', s)})"

    if subject.parents:
        lines.append("Its repo is nested inside: " + ", ".join(label(s) for s in subject.parents[:2]) + ".")
    if subject.children:
        lines.append("Sub-projects nested in its repo: " + ", ".join(label(s) for s in subject.children) + ".")
    if subject.renamed_from:
        lines.append("It was renamed from: " + ", ".join(subject.renamed_from) + ".")
    if subject.renamed_to:
        lines.append("It is the former id of: " + ", ".join(label(s) for s in subject.renamed_to) + ".")
    if subject.mentioned_by:
        lines.append("The git history of these registered projects mentions this slug (a likely "
                     "rename or absorption): " + ", ".join(label(s) for s in subject.mentioned_by) + ".")
    lines.append("How to choose: a folder named after the project itself (any case, CamelCase or an "
                 "acronym of its name) is its home. Otherwise the folder of the project it was renamed "
                 "to or from. Otherwise, for a sub-project, its parent's folder or its own sub-project's "
                 "folder. A registered project with no folder at all gets a new one. Hold only when the "
                 "evidence points nowhere.")
    return "\n".join(lines)


def ask_jev(
    subject: Subject,
    candidates: list[Candidate],
    create: str | None,
    registry: dict[str, dict[str, Any]] | None,
    *,
    key: str,
    url: str,
    timeout: float = 45,
    post: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    criteria: dict[str, str] = {}
    for c in candidates:
        criteria[c.target] = f"existing folder {c.target} — evidence: {'; '.join(c.reasons)}"
    if create:
        criteria[create] = (f"create a new folder {create[len(CREATE_PREFIX):]}: this registered "
                            "project has no folder of its own and none of the listed folders belongs to it")
    criteria[HOLD] = ("the evidence points to no listed folder and the project is not registered, "
                      "or the slug looks like noise")
    body = {
        "model": os.environ.get("JEV_MODEL") or "jev-latest",
        "state": describe(subject, registry),
        "questions": {
            "target": {
                "type": "choice",
                "instructions": "Which vault folder is this project's home?",
                "criteria": criteria,
            }
        },
    }
    d = (post or _post_json)(url, body, key, timeout)
    answer = (d.get("answers") or {}).get("target") or {}
    return {
        "choice": answer.get("choice"),
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities"),
        "model": d.get("model"),
    }


# ── decision ───────────────────────────────────────────────────────────────

def decide(
    slug: str,
    hold_kind: str,
    *,
    vault_root: Path,
    mapping: dict[str, str],
    registry: dict[str, dict[str, Any]] | None,
    projects_dir: str = "Projects",
    min_confidence: float = 0.65,
    agree_confidence: float = 0.4,
    use_jev: bool = True,
    jev_url: str = DEFAULT_JEV_URL,
    key_fn: Callable[[], str | None] = jev_key,
    post: Callable[..., dict] | None = None,
    known_fingerprint: str | None = None,
    git_search: bool = True,
) -> Decision:
    subject = build_subject(slug, hold_kind, mapping, registry, git_search=git_search)
    candidates = score_candidates(subject, vault_root, projects_dir, mapping, registry)
    create = create_option(subject, projects_dir, vault_root)
    if create and any(c.score >= STRONG for c in candidates):
        # A strong existing home exists: never offer a sibling duplicate.
        create = None
    waiting_on = [s for s in subject.parents[:1] + subject.renamed_from + subject.renamed_to
                  if s not in mapping and s != slug]
    if create and waiting_on:
        # The parent / renamed relative has no folder yet; once it does, this
        # slug is re-asked (Runtime re-schedules relatives), so wait instead
        # of creating a sibling it would duplicate.
        create = None
    fp = fingerprint(subject, candidates, create, mapping)
    if known_fingerprint and known_fingerprint == fp:
        # Same evidence as the last "hold": asking again would only repeat it.
        return Decision(slug, hold_kind, "skip", None, None, "none",
                        "evidence unchanged since the last hold", [], fp)
    cand_json = [asdict(c) for c in candidates]
    if create:
        cand_json.append({"target": create, "score": None, "reasons": ["registered project, no existing folder"]})

    def hold(reason: str, decider: str, conf: float | None = None, jev: dict | None = None) -> Decision:
        return Decision(slug, hold_kind, "hold", None, conf, decider, reason, cand_json, fp, jev)

    def take(target: str, decider: str, conf: float | None, reason: str, jev: dict | None = None) -> Decision:
        if target.startswith(CREATE_PREFIX):
            return Decision(slug, hold_kind, "create", target[len(CREATE_PREFIX):], conf, decider, reason, cand_json, fp, jev)
        return Decision(slug, hold_kind, "map", target, conf, decider, reason, cand_json, fp, jev)

    if registry is None and not candidates:
        return hold("registry unreachable and no vault evidence", "none")

    key = key_fn() if use_jev else None
    if key:
        try:
            jev = ask_jev(subject, candidates, create, registry, key=key, url=jev_url, post=post)
        except Exception as exc:  # noqa: BLE001 - fall back to the deterministic rule
            jev = {"error": f"{type(exc).__name__}: {exc}"}
        choice, conf = jev.get("choice"), jev.get("confidence")
        if not (isinstance(conf, (int, float)) and 0.0 <= float(conf) <= 1.0):
            conf = None
        valid = {c.target for c in candidates} | ({create} if create else set()) | {HOLD}
        if isinstance(choice, str) and choice in valid:
            if choice == HOLD:
                return hold("Jev chose to hold", "jev", conf, jev)
            # Jev alone must clear min_confidence; Jev agreeing with the
            # independent evidence (the top-scored folder, or "create" when no
            # folder scored) only has to clear agree_confidence.
            top = candidates[0] if candidates else None
            tied = len(candidates) > 1 and candidates[1].score == candidates[0].score
            agrees = (top is not None and choice == top.target and top.score >= 0.6 and not tied) or (
                choice.startswith(CREATE_PREFIX) and not any(c.score >= 0.4 for c in candidates)
            )
            bar = agree_confidence if agrees else min_confidence
            if conf is None or conf < bar:
                return hold(f"Jev chose {choice} at {conf}, below {bar} "
                            f"({'agrees' if agrees else 'disagrees'} with the evidence)", "jev", conf, jev)
            if choice.startswith(CREATE_PREFIX) and registry is None:
                return hold("refusing to create a folder while the registry is unreachable", "jev", conf, jev)
            return take(choice, "jev", conf,
                        f"Jev chose {choice} ({conf}){', agreeing with the evidence' if agrees else ''}", jev)
        # Unusable answer: fall through to the deterministic rule, keeping Jev's reply.
        det = _deterministic(candidates, create, registry)
        if det:
            return take(det[0], "deterministic", det[1], det[2] + " (Jev unusable)", jev)
        return hold("Jev gave no usable answer and no unambiguous match", "none", None, jev)

    det = _deterministic(candidates, create, registry)
    if det:
        return take(det[0], "deterministic", det[1], det[2])
    return hold("no Jev key and no unambiguous deterministic match", "none")


def _deterministic(
    candidates: list[Candidate], create: str | None, registry: dict | None
) -> tuple[str, float, str] | None:
    """Only the unambiguous case: one strong match well clear of the next."""
    if candidates:
        top = candidates[0]
        runner = candidates[1].score if len(candidates) > 1 else 0.0
        if top.score >= 0.9 and (runner < 0.6 or (top.score >= 1.0 and runner < 0.9)):
            return top.target, top.score, f"unambiguous match: {'; '.join(top.reasons)}"
    return None


def order_slugs(slugs: list[str], registry: dict[str, dict[str, Any]] | None) -> list[str]:
    """Parents before children, so a child sees its parent's fresh mapping."""
    def depth(s: str) -> tuple[int, str]:
        repo = _repo((registry or {}).get(s))
        return (len(repo.parts) if repo else 99, s)

    return sorted(set(slugs), key=depth)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
