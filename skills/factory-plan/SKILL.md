---
name: factory-plan
description: Check that a project's MVP and design documents are clear, correctly understood and complete enough for agents to build from, record the user's answers, then create the GitHub issues. Factory step 4.
argument-hint: "<path to MVP doc> [--vision <path>]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(gh issue list *) Bash(gh label list *)
---

# /factory-plan: from MVP document to issues

You are the **manager**. You coordinate agents and harness commands; you don't judge the documents or write
issues yourself. The goal is that the documents are clear and complete enough that builders (and the
`question-resolver`) can answer their own questions later. Issue creation is the easy part and has no approval
gate; the care goes into steps 2 to 4.

Arguments: `$ARGUMENTS`
- The first path is the MVP document. If it's missing, ask for it and stop.
- `--vision <path>`: the vision document, if the user has one. If none exists and the MVP document has no vision
  section, say so: product questions can't be settled without one. Ask whether to continue anyway.

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

Work folder: `${CLAUDE_PROJECT_DIR}/.factory/plans/<YYYYMMDD-HHMM>/` (create it).

## Steps

1. **Review the documents.** Spawn `mvp-reviewer` with the document paths, the work folder and the harness
   command. It writes `understanding.md` and `gaps.json` there.
2. **Confirm the understanding.** Show the user `understanding.md` and ask (AskUserQuestion) whether it matches
   what they meant: correct, or wrong in places (they say where). If wrong, note the corrections, record each
   with `H factory decide ... --kind product --by user --why "clarifies the MVP document"`, and re-run step 1 so
   the gaps reflect the corrected reading. At most 2 rounds.
3. **Close the gaps.** Read `gaps.json`. Show contradictions first. Then put the gaps to the user in batches of
   up to 4 questions (AskUserQuestion), product questions and anything that cascades first. Each question shows
   the reviewer's proposal as the recommended option; the user can accept it, answer differently, or say
   "skip" (the gap stays open and is noted on the issues). Record every answer immediately with
   `H factory decide --question ... --answer ... --by user --kind <product|implementation> --why ... --source ...`.
   Offer once to accept all remaining low-impact implementation proposals in bulk; if accepted, record them with
   `--by assumed` and the reviewer's reasoning. Never answer a product question for the user.
4. **Say whether the documents are ready.** One or two lines: what's settled, what's still open and which issues
   it will touch. Ask whether to continue to planning or to stop here and let the user update the documents.
5. **Plan and create.** Spawn `factory-planner` with the document paths, `.factory/decisions.md`, the work folder's
   `issues.json` path and the harness command. Then run `H factory issues-create <issues.json>`: it checks the list,
   creates the factory labels if missing, creates the issues dependencies first with `Depends on #N` lines,
   and skips titles that already exist. If it reports problems, send them back to the planner once; if they
   persist, stop and tell the user. Don't add the `todo` label and don't fix the draft yourself.
6. **Report.** The created issue numbers with titles and dependencies as a compact list, the planner's open notes,
   and the paths of `.factory/decisions.md` and `.factory/log.jsonl`. Nothing runs automatically: the user tells
   the manager which issues to work on.
