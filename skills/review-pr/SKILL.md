---
name: review-pr
description: Review a PR together with the PRs it depends on in the project's other repos (cross-lab reviewer + verifier), then create pending GitHub reviews you edit and submit yourself.
argument-hint: "<PR URL or repo#number> [--only]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *)
---

# /review-pr: review a PR set

You are the **manager** of a PR review. You coordinate harness commands and subagents; you don't read
diffs or source yourself. Keep your messages short.

Arguments: `$ARGUMENTS`. `--only` means review just this PR, without discovering linked PRs.
If no PR was given, ask for one and stop.

`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

Nothing in this flow checks out code or touches the user's branches: PRs are fetched into hidden
`refs/harness/pr/<n>` refs and read from git.

## 1. Load the PR set
Run `H pr start <ref>` (add `--no-discover` for `--only`). It loads the PR, then finds every open PR in
the project's repos that is essential to it:
- PRs linked from its description or comments (and PRs linked from those),
- PRs whose description links back to it,
- the PR whose branch another PR's `package.json` installs as a dependency from one of the project's repos.
It prints the set with the reason each PR is included, linked PRs that are already merged or closed,
**POSSIBLY RELATED** PRs it didn't include, and warnings.

If an active run blocks it, ask the user whether to end it (`H run end --status aborted`).

If there are POSSIBLY RELATED PRs, ask with AskUserQuestion (multiSelect) which to include, then
`H pr add <repo>#<n>` for each one chosen. Relay every WARNING to the user. For example, a warning
that the app installs a component-library branch with no open PR means the set is incomplete.

## 2. Review
1. `H review --round 1`. If it prints `FALLBACK`, spawn the `harness:claude-reviewer` subagent for round 1 and
   tell it this is a **PR review run** (the run directory, the harness path, round 1).
2. Spawn the `harness:verifier` subagent for round 1. Tell it this is a **PR review run**: the code is not
   checked out, so it reads files with `git show <ref>:<path>` (`H pr show` prints the exact
   commands) and checks consumers in the other repos with `git grep ... origin/<base>`.
3. `H verdict --round 1` (prints `DECISION: DRAFT`).

## 3. Draft
Run `H pr draft`. Show the user, per PR: the counts it prints, and the 3–5 most important confirmed
items (read them from `<run_dir>/draft-<repo>-<n>.md`; those files are short). Give the draft
paths so they can read everything.

## 4. Post (only with the user's OK)
Ask with AskUserQuestion: "Create pending reviews on GitHub?" options:
- "Yes, create pending reviews": comments stay private to the user until they submit on GitHub.
- "Change something first": take their edits, apply them to the draft JSON files
  (`<run_dir>/draft-*.json`: `body` and `comments[]`), then ask again.
- "No, keep the drafts local".

If yes: `H pr post`. Relay each result line. If a PR was skipped because the author pushed new
commits, offer to re-run the review. Remind the user to open each PR's "Files changed" tab to edit and
submit their pending review (Approve / Request changes / Comment). The choice of verdict is theirs.

## 5. Finish
`H run end --status done`. Then ask: "How useful was this review?" options "5 - posted as is",
"4 - minor edits", "3 - significant edits", "2 - mostly rewrote it". Record with `H rate <score> --note "..."`.

## Rules
- Never post, comment, approve or request changes any other way. The guard blocks `gh pr review`,
  `gh pr comment` and `gh api` writes. `H pr post` is the only path, and only after the user says yes.
- Never check out PR branches or modify the user's working trees.
