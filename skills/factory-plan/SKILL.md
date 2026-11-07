---
name: factory-plan
description: Draft GitHub issues from a project's MVP document with the factory-planner, get the user's approval of the list, then create the issues (dependencies first). Factory step 4.
argument-hint: "<path to MVP doc> [--vision <path>] [--todo]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(gh issue list *) Bash(gh label list *)
---

# /factory-plan: MVP document to approved issues

You are the **manager**. You coordinate; you don't write the issues yourself. Keep your context small: pass file
paths, read only the short outputs of harness commands.

Arguments: `$ARGUMENTS`
- The first path is the MVP document. If it's missing, ask for it and stop.
- `--vision <path>`: the vision document, if the user has one.
- `--todo`: label every created issue `todo` (default: no label, the user picks the pool later).

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

## Steps

1. **Output path.** `PLAN=${CLAUDE_PROJECT_DIR}/.factory/plans/<YYYYMMDD-HHMM>/issues.json`. Create the folder.
2. **Draft.** Spawn the `factory-planner` agent. Tell it: the MVP document path, the vision path if any, `PLAN`,
   the harness command, and (on later rounds) the user's feedback verbatim. If `models` overrides apply to it,
   pass them as in /ship.
3. **Show the list.** Run `H factory issues-validate PLAN`. If it fails, send the problems back to the planner once;
   if it still fails, stop and tell the user. Otherwise show the user the output (key, title, criteria count,
   dependencies, notes), plus the planner's short summary. Offer to open `PLAN` for the full text.
4. **Approval.** Ask with AskUserQuestion: approve, change (they say what), or abort. On change, go back to step 2
   with their words as feedback (at most 3 rounds, then ask whether to continue by hand-editing `PLAN`). The user
   may also edit `PLAN` themselves; re-validate after.
5. **Create.** Only after an explicit approve: `H factory issues-create PLAN [--todo]`. Use `--dry-run` first if
   the user asked to see the order. The command creates the factory state labels if missing, skips issues whose
   title already exists, and writes `Depends on #N` lines.
6. **Report.** Show the created issue numbers and the `.factory/log.jsonl` path. Nothing runs automatically; the
   user chooses which issues to label `todo` and run with `/ship`.

Never create issues before the approval, never add `todo` unless `--todo` was given, and never edit the draft
yourself to "fix" the planner's work: send it back.
