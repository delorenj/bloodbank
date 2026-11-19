"""Move or merge a vault folder without breaking what points into it.

`merge_folder` moves every file of SRC into DST. A file that is already at the
same path in DST with the same bytes is dropped. One with different bytes gets
the projector's usual `-<sha8>` suffix, so nothing is ever overwritten. SRC
directories are removed only once they are empty.

`rewrite_links` then fixes markdown links and path-qualified wikilinks across
the vault: links into a moved file, and the relative links of the moved files
themselves. A link whose target does not exist is never touched, so links to
paths outside the vault (a repo checkout, say) keep their text.

The projector's `--move-folder` uses both and also remaps the slugs and its
projection rows (main.py).

Standard library only.
"""
from __future__ import annotations

import filecmp
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

SKIP_DIRS = {".obsidian", ".trash", ".git", ".stversions", "node_modules"}
MD_LINK_RE = re.compile(r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|[^)\s]+)((?:\s+\"[^\"\n]*\")?\))")
WIKI_RE = re.compile(r"(!?\[\[)([^\]|#\n]+)((?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\])")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class MoveError(ValueError):
    pass


def unsafe_rel(target: str) -> str | None:
    """Why a vault-relative folder path is unsafe, or None."""
    parts = Path(target).parts
    if not target or target.startswith(("/", "\\")) or ".." in parts or any(p.startswith(".") for p in parts):
        return f"unsafe vault path {target!r}"
    return None


def _sha8(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def merge_folder(root: Path, src: str, dst: str) -> dict:
    """Move every file under root/src into root/dst. Returns moves + counts."""
    for t in (src, dst):
        if why := unsafe_rel(t):
            raise MoveError(why)
    root = Path(os.path.abspath(root))
    s, d = root / src, root / dst
    if s.is_symlink() or not s.is_dir():
        raise MoveError(f"{src} is not a directory")
    if d.resolve().is_relative_to(s.resolve()) or s.resolve().is_relative_to(d.resolve()):
        raise MoveError(f"{src} and {dst} are the same or nested folders")
    created = False
    if d.exists():
        if d.is_symlink() or not d.is_dir():
            raise MoveError(f"{dst} exists and is not a directory")
    else:
        if not d.parent.is_dir() or d.parent.is_symlink():
            raise MoveError(f"cannot create {dst}: parent missing")
        os.mkdir(d, 0o755)
        created = True

    moves: dict[str, str] = {}
    deduped, renamed = 0, []
    for dirpath, dirnames, filenames in os.walk(s, followlinks=False):
        rel = Path(dirpath).relative_to(s)
        target_dir = d / rel
        if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
            raise MoveError(f"{target_dir} is in the way")
        target_dir.mkdir(exist_ok=True)
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                filenames.append(name)  # move the link itself, never walk it
        dirnames[:] = [n for n in dirnames if not (Path(dirpath) / n).is_symlink()]
        for name in sorted(filenames):
            sp = Path(dirpath) / name
            dp = target_dir / name
            if os.path.lexists(dp):
                regular = sp.is_file() and not sp.is_symlink() and dp.is_file() and not dp.is_symlink()
                if regular and filecmp.cmp(sp, dp, shallow=False):
                    os.unlink(sp)
                    moves[str(sp)] = str(dp)
                    deduped += 1
                    continue
                suffix = _sha8(sp) if sp.is_file() and not sp.is_symlink() else "link"
                dp = target_dir / f"{Path(name).stem}-{suffix}{Path(name).suffix}"
                if os.path.lexists(dp):
                    raise MoveError(f"{dp} already exists")
                renamed.append(str(dp.relative_to(root)))
            os.rename(sp, dp)
            moves[str(sp)] = str(dp)
    for dirpath, _dirs, _files in os.walk(s, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return {"src": src, "dst": dst, "created": created, "moved": len(moves) - deduped,
            "deduped": deduped, "renamed": renamed, "src_removed": not s.exists(), "moves": moves}


def _iter_notes(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [n for n in dirnames if n not in SKIP_DIRS and not n.startswith(".")]
        for name in filenames:
            if name.endswith(".md"):
                yield Path(dirpath) / name


def rewrite_links(root: Path, moves: dict[str, str]) -> list[str]:
    """Fix links that pointed into moved files (and links inside moved notes)."""
    if not moves:
        return []
    root = Path(os.path.abspath(root))
    back = {v: k for k, v in moves.items()}
    moved_old = set(moves)

    def existed(old: str) -> bool:
        # In the pre-move world: a moved file, or a file that is still where it was.
        return old in moved_old or (old not in back and os.path.isfile(old))

    changed: list[str] = []
    for note in _iter_notes(root):
        new_note = str(note)
        old_note = back.get(new_note, new_note)
        old_dir, new_dir = os.path.dirname(old_note), os.path.dirname(new_note)
        note_moved = old_note != new_note

        def md(m: re.Match) -> str:
            raw = m.group(2)
            bracketed = raw.startswith("<")
            target = raw[1:-1] if bracketed else raw
            if SCHEME_RE.match(target) or target.startswith("#"):
                return m.group(0)
            path, sep, frag = target.partition("#")
            decoded = unquote(path)
            for base, relative in ((old_dir, True), (str(root), False)):
                old_t = os.path.normpath(os.path.join(base, decoded))
                if existed(old_t):
                    break
            else:
                return m.group(0)
            new_t = moves.get(old_t, old_t)
            if new_t == old_t and not (note_moved and relative):
                return m.group(0)
            base_old, base_new = (old_dir, new_dir) if relative else (str(root), str(root))
            if os.path.relpath(old_t, base_old) == os.path.relpath(new_t, base_new):
                return m.group(0)  # same relative relationship: keep the author's spelling ("./x.md")
            new_path = os.path.relpath(new_t, new_dir) if relative else os.path.relpath(new_t, root)
            if not bracketed and ("%" in path or " " in new_path):
                new_path = quote(new_path, safe="/._-~()!,;=+&'")
            out = new_path + (sep + frag if sep else "")
            if out == target:
                return m.group(0)
            return m.group(1) + (f"<{out}>" if bracketed else out) + m.group(3)

        def wiki(m: re.Match) -> str:
            target = m.group(2).strip()
            if "/" not in target:
                return m.group(0)  # name-only links resolve by name; a move keeps them
            for cand, ext in ((target, ""), (target + ".md", ".md")):
                old_t = os.path.normpath(os.path.join(str(root), cand))
                if existed(old_t):
                    break
            else:
                return m.group(0)
            new_t = moves.get(old_t)
            if not new_t:
                return m.group(0)
            out = os.path.relpath(new_t, root)
            if ext and out.endswith(ext):
                out = out[: -len(ext)]
            return m.group(1) + out + m.group(3)

        try:
            text = note.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = WIKI_RE.sub(wiki, MD_LINK_RE.sub(md, text))
        if new_text != text:
            tmp = note.with_name(f".{note.name}.relink.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.chmod(tmp, note.stat().st_mode & 0o7777)
            os.replace(tmp, note)
            changed.append(str(note.relative_to(root)))
    return changed
