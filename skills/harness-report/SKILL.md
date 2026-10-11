---
name: harness-report
description: Show cost and performance metrics for the /ship pipeline (per agent, per model, reviewer precision, gate pass rates, recent runs).
argument-hint: "[--days N] [--run RUN_ID]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *)
---

Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR} report $ARGUMENTS` and
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR} models`.

Show both outputs to the user as-is (they're tables), then add at most four short observations, for example:
- the most expensive stage and whether a cheaper model is worth trying there
  (`harness models set <agent> <model>`),
- reviewer precision below ~50% (too many false positives) or many "missed" issues
  (reviewer too weak), and whether to try another provider in .claude/harness.toml,
- a low engineer first-attempt gate pass rate (plans too vague, or engineer model too weak),
- low run ratings.
Say when there isn't enough data yet (fewer than ~5 runs) to draw conclusions.
