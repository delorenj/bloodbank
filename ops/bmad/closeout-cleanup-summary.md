# Closeout cleanup status summary

Use this read-only helper to quickly inspect cleanup quality across recent closeout artifacts.

```bash
mise run bmad:closeout-cleanup-summary -- --limit 10
```

Optional custom evidence directory:

```bash
mise run bmad:closeout-cleanup-summary -- --evidence-dir _bmad_output/evidence --limit 25
```

Output includes per-artifact:
- PR id (`pr`)
- overall closeout status (`overall_status`)
- normalized cleanup status (`cleanup_local_branch_status`)
- local-branch deletion signal (`cleanup_local_branch_deleted`)
- follow-up command count (`cleanup_followup_count`)
- warning count (`warning_count`)
