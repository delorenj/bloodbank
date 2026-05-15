# Primary checkout recovery runbook (diverged `main`)

Use this when your primary checkout is ahead/behind `origin/main` and merged BMAD helper files are missing locally.

## 0) Read-only diagnostics (required)

```bash
mise run bmad:primary-recovery-check -- --repo /path/to/primary/checkout
```

This emits:
- `ahead` / `behind`
- `patch_equivalent_divergence`
- `helper_local_exists`
- `helper_on_origin_main`
- `recommended_path`

## 1) Preferred backup-first helper (recommended)

Run helper in read-only mode first:

```bash
mise run bmad:align-main-with-backup -- --repo /path/to/primary/checkout
```

If output looks correct, run apply mode (creates backup branch + bundle + reset):

```bash
mise run bmad:align-main-with-backup -- --repo /path/to/primary/checkout --apply
```

## 1b) Manual safety anchor (fallback)

```bash
cd /path/to/primary/checkout

TS="$(date -u +%Y%m%dT%H%M%SZ)"
git branch "backup/main-divergence-${TS}" main
git bundle create "/tmp/bloodbank-main-${TS}.bundle" main "backup/main-divergence-${TS}"
```

Do **not** skip this backup.

## 2) Recovery paths

### A) Patch-equivalent divergence + helper present

```bash
python3 ops/bmad/reconcile_main_divergence.py --repo /path/to/primary/checkout --apply
```

### B) Patch-equivalent divergence + helper missing locally but exists on `origin/main`

1. Align local `main` to `origin/main` **after backup**:

```bash
git checkout main
git fetch origin main
git reset --hard origin/main
```

2. Re-run diagnostics and helper:

```bash
mise run bmad:primary-recovery-check -- --repo /path/to/primary/checkout
python3 ops/bmad/reconcile_main_divergence.py --repo /path/to/primary/checkout --apply
```

### C) Non patch-equivalent divergence (`recommended_path=manual_rebase_or_backup_then_reset`)

Use one of:
- rebase/local-history repair path, or
- helper-driven backup + reset to canonical `origin/main`:

```bash
mise run bmad:align-main-with-backup -- --repo /path/to/primary/checkout --apply
```

This remains a **manual decision point** and should be ticketed/evidenced.

## 3) Post-recovery verification

```bash
git status -sb
python3 cli/bb.py repo-health --json --require-clean-worktree
```

Target state:
- `main...origin/main` (no ahead/behind)
- `worktree_dirty=false`
- helper files available locally.

## 4) Post-recovery artifact hygiene

Preview cleanup plan first:

```bash
mise run bmad:recovery-artifact-cleanup -- --repo /path/to/primary/checkout
```

Apply cleanup when ready (example keeps latest 1 branch + 1 bundle):

```bash
mise run bmad:recovery-artifact-cleanup -- --repo /path/to/primary/checkout --keep-branches 1 --keep-bundles 1 --apply
```
