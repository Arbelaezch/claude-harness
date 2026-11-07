---
name: factory-issue
description: Build one GitHub issue end to end in a headless factory session (the /ship pipeline without human checkpoints), open the PR, and record the outcome. Started by `harness factory run`, one fresh session per issue.
argument-hint: "<issue number>"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit *) Bash(git push *) Bash(gh pr create *) Bash(gh issue view *)
---

# /factory-issue: one issue, unattended

You are the factory's **manager for exactly one issue**, running headless: nobody is watching and nobody can answer
a question. Your context ends with this issue, so don't carry anything forward and never start another issue.
Your job ends in exactly one of two ways: a pull request is open and recorded (`factory done`), or the issue is
recorded as blocked (`factory block`). A session that ends any other way is treated as a failure.

Arguments: `$ARGUMENTS`  (the issue number `N`)

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

## How to work

Read `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` and follow it for issue `#N`, as if the user had run
`/ship #N --no-grill`, with these changes. The ship skill stays the single source for the pipeline; this file only
changes where humans were involved.

1. **No grill, no plan checkpoint.** Skip steps 1 and 4 of /ship: the documents were reviewed and answers recorded
   in `.factory/decisions.md` before the issue existed. Treat the plan as approved. Read that decisions file and the
   issue's linked documents before planning; pass the relevant decisions to the planner and engineers.
2. **Nobody to ask.** Never use AskUserQuestion. Where /ship says to ask the user or relay `NEEDS USER`, first see
   whether `.factory/decisions.md` or the issue's documents settle it (quote the line when you use it). If they
   do, continue. If they don't:
   - run `H factory block N --situation "<at most 3 sentences>" --need "<one decision or action>"
     [--option ... --option ... --recommend <k>] --detail "<run dir path or log>"`, then stop. Use `--kind QUESTION`
     for a product or direction question only the user can settle, `BLOCKER` otherwise.
   - Do the same, without trying a workaround, for: a dirty tree you didn't create, changed dependencies, gates
     still failing after the retries /ship allows, an ESCALATE verdict, two failed fix attempts, a command the guard
     blocked, or any error you can't fix by following /ship.
3. **Finish with a pull request.** When the verdict is DONE and the summary is written:
   1. Stage only the files in the diff (`git status`, then `git add <paths>`; never `git add -A` or `.`), and commit
      once with a message that names the issue, e.g. `Add export endpoint (#N)`. Don't skip hooks or amend.
   2. Push the work branch: `git push origin <branch>`. Never push the base or a protected branch.
   3. Open the PR: `gh pr create --base <base> --head <branch> --title "<title>" --body-file <run_dir>/pr-<repo>.md`.
      The draft already has `Closes #N`. Multi-repo issues get one PR per repo, linked as the draft says.
   4. `H run end --status done`, then `H factory done N --pr <PR url>`.
   Do not merge. Merging is the user's, or build step 6's.
4. **If anything in step 3 fails** (push rejected, PR creation fails): `H factory block N` with what happened.
5. **Rules that still apply:** subagents never commit or push; the guard enforces it. Keep your own output short.
