# Software Factory: design

Status: draft, 2026/11/06. Nothing here is built yet except `scripts/usage.py` and `scripts/statusline_capture.py`.

## 1. What this is

A layer above the harness. I point it at a project with an MVP document. It creates issues, and when I
tell it which issues to work on, it runs harnesses on them one at a time: build, test, review, open a PR,
merge. It only comes to me for decisions that are mine to make. It pauses when plan usage runs out and
resumes when it comes back.

It is for **personal projects**. Professional work keeps the existing harness rules (section 11).

Goals
- Maximum automation on personal projects, with the product vision staying mine.
- Modular: roles, harness recipes and the factory engine are separate and swappable.
- One place to edit anything: a role is one file, a recipe is one file.
- Never surprise me: every message to me is short, standardised and says what I need to decide.

Not goals (v1): see section 13.

## 2. Layers

```
factory        picks issues, runs harnesses, gates on usage, merges, talks to me
  harness      a recipe: which roles, in what order, with which gates and limits
    role       one agent file: the single source of truth for that job
guard          hook that decides who may commit, push and merge (section 11)
```

Rules that keep this modular
1. A role file holds the instructions for its job. A recipe never copies role instructions into itself.
   It only names roles.
2. A recipe holds order, gates, limits and sign-off rules. A role never encodes workflow.
3. The factory calls recipes by name. It never calls roles directly, except the factory-level roles
   in section 4.
4. All limits (review rounds, usage thresholds, retries) live in config, not code.

## 3. Repository layout

Everything lives in one repo, `software-factory` (renamed from `claude-harness`, section 14).

```
agents/              one markdown file per role (existing five plus the new ones)
harnesses/           one TOML recipe per harness: ship, review-pr, ci-fix, conflict-resolve
factory/             the engine (Python, standard library only)
scripts/             harness.py (existing), usage.py, statusline_capture.py
skills/              /ship, /review-pr and so on (existing)
docs/                this file
tests/
```

Per-project files live in the **target repo**, not here:

```
<project>/.factory/project.toml     MVP doc path, vision doc path, check command, merge policy, labels
<project>/.factory/log.jsonl        append-only factory log (state for resume, section 8)
<project>/CLAUDE.md                 project conventions the agents read
```

The factory takes a project path as an argument. One factory run handles one project.

## 4. Roles

| Role | Job | Status |
|---|---|---|
| `scout` | Maps relevant code for a task | exists |
| `planner` | Turns an issue plus scout context into a task plan | exists |
| `engineer` | Implements one task or fixes review findings | exists |
| `claude-reviewer` | Fallback reviewer | exists |
| `verifier` | Checks reviewer findings against the code | exists |
| `factory-planner` | Reads the MVP doc, creates issues with acceptance criteria and dependencies | new |
| `issue-selector` | Picks the next issue from the `todo` pool | new |
| `question-resolver` | Answers a question from the source docs, or decides it needs me (section 6) | new |
| `human-liaison` | Sits between the factory and me for every question and blocker (section 7) | new |
| `ci-fixer` | Diagnoses a failed CI run, comments on the PR, attempts one fix | new |
| `conflict-resolver` | Resolves a merge conflict using both sides' context | new |
| `harness-cleanup` | End-of-run cleanup. First job: re-check out-of-scope concerns and file the survivors | new |

Git permissions by role: only the factory manager and the roles below may commit, push, create PRs or
merge, and only in factory mode (section 11). `scout`, `planner` and the reviewers stay read-only
for git. `engineer` may commit to its issue branch. `ci-fixer` and `conflict-resolver` may push to the
PR branch they were launched on. The factory manager creates and merges PRs.

## 5. Harness recipes

A recipe is a short TOML file. Sketch for `harnesses/ship.toml`:

```toml
name = "ship"
mode = "factory"                       # "factory" or "professional" (section 11)

[flow]
stages = ["scout", "plan", "implement", "review", "pr"]

[roles]
scout = "scout"
plan = "planner"
implement = "engineer"

[gates]                                # deterministic, run before any review
command = "${project.check_command}"   # same command CI runs

[review]
reviewers = ["claude-reviewer"]        # ordered list, swap freely
verifier = "verifier"
max_rounds = 3                         # matches the current harness setting
signoff = "all"                        # "all" | "majority" | "none"
on_disagreement = "block"              # reviewer vs verifier disagreement at the cap raises a blocker

[limits]
retries = 0                            # a failed or blocked run is never re-run; it is raised to me
```

Review stays modular on purpose: the reviewer list, the round cap and the sign-off rule are all in the
recipe, so reworking the review step is a recipe edit. As project CI matures, soften review by lowering
`max_rounds` or loosening `signoff`.

Other recipes
- `review-pr`: existing behaviour for PRs, unchanged.
- `ci-fix`: `ci-fixer` diagnoses, comments on the PR with diagnosis and attempted fix, pushes one fix.
  One attempt only. If CI fails again, the factory raises a blocker.
- `conflict-resolve`: `conflict-resolver` merges with both sides' intent in context, runs the gates, pushes.

CI vs review: the ship recipe runs only local gates. Remote CI runs after the PR is open and is the final
authority before merge. The local gate command and the CI workflow must call the same check command so
they cannot drift.

## 6. Questions: ask or decide

Every open question goes to `question-resolver` before it can reach me. It classifies on two axes:

- Domain: **product** (direction, use case, UX) or **implementation**.
- Reversibility: does the answer cascade (data model, public API shape, anything other issues build on)
  or is it cheap to change later?

It returns one of three outcomes:

| Outcome | When | Recorded |
|---|---|---|
| answered | The source docs settle it. Quote the line. | On the issue |
| assumed | Implementation, low impact, reversible, high certainty. Nothing else. | On the issue and in the PR description |
| escalate | All product questions, anything that cascades, anything it is unsure about | Goes to me via `human-liaison` |

The bar for `assumed` is high. When unsure it escalates. Implementation freedom is wide, product freedom
is narrow: the vision stays mine.

Each project needs a vision source (a section in the MVP doc or a `VISION.md`). `question-resolver`
treats it as authoritative for product questions.

## 7. Talking to me

All messages from the factory to me pass through `human-liaison`: questions, blockers, usage
problems, completion notices. The goal is one standard shape that is short and decidable.

Message format
```
[QUESTION | BLOCKER | DONE]  <project> #<issue>
What is going on:   at most 3 sentences
What I need:        one specific decision or action
Options:            2-3 choices, one marked as recommended (when applicable)
Detail:             link to the raw CI log, agent output or issue comment
```

v1 behaviour is light: restate what the CI or agent reported, in this format. Smarter summarising comes later
without changing the format.

Blockers (raised to me, never retried)
- A question `question-resolver` escalates.
- A gate still failing after the review round cap.
- CI failing a second time after `ci-fixer`'s attempt.
- Reviewer and verifier disagreeing at the cap.
- The usage check returning `unknown` repeatedly (default: 3 times in a row).
- A harness run failing or getting blocked.

Not blockers: merge conflicts (go to `conflict-resolver`), a single CI failure (goes to `ci-fixer`), hitting a
usage limit (pause and resume).

Delivery goes through one function, `notify(message)`, with channels behind it.
- `terminal`: default.
- `push`: off by default, switchable per run and mid-session. Not built in v1, scaffolding only. See
  "Phone access" below for why.
- Adding a channel means adding one class. Nothing else changes.

Phone access (status 2026/11/06)
- The manager is meant to run as a long-lived interactive session started with `claude --remote-control`.
  Workers stay headless children. `human-liaison` asks through `AskUserQuestion` in the manager session.
- Tested and working: Remote Control connects, the session shows in the Claude app's Code tab, and a question
  posted by the session can be answered from the app. So replying from the phone works.
- Tested and not working: phone push notifications. `PushNotification` returned "Mobile push requested" and
  `AskUserQuestion` was posted while I was away from the terminal, but neither produced a push or a Mac
  notification. Only the push sent when Remote Control connects arrived. The terminal and app were on the
  same account (Pro plan), both push toggles in `/config` were on, and no "No mobile registered" warning
  showed. A feedback report was drafted. Tracked in a GitHub issue.
- A mismatch between the terminal's login and the phone app's account causes the "No mobile registered"
  warning, so both must use the same claude.ai account. The factory's usage limits are that account's too.
- Until this is fixed, the terminal is the notification channel, and I check the Code tab in the app by hand.
  No interim push service is planned.

A blocker stops only the blocked issue and anything that depends on it. The factory keeps working other
independent issues in my directed group. Questions are batched so I am not pinged repeatedly.

## 8. State and labels

State lives in GitHub plus one log file. No database.

Labels
- `todo`: in the candidate pool. I add it, or the factory adds it when I name issues directly.
- `in-progress`: a harness is running on it.
- `awaiting-merge`: PR open, waiting on CI or on me.
- `blocked`: raised to me.
- `backlog`: created by `harness-cleanup`. Never worked until I change it to `todo`.

`.factory/log.jsonl` records every factory decision (issue picked, harness started and finished, usage pause
and resume, question outcome, merge). It is append-only. On start the factory replays it plus issue labels
to resume after a crash, pause or closed laptop.

Dependencies are written in each issue body as `Depends on #N`. The selector reads them.

## 9. The run loop

```
start(project, merge_policy?)             # asks once: auto-merge or wait for my review (overridable per session)
loop:
  usage = usage.check()                   # scripts/usage.py
  if pause:   sleep until resume_at, then continue       # no prompt to me
  if unknown: count it; at 3 in a row raise a blocker
  pool = issues labelled todo, not blocked, dependencies merged
  if pool is empty: finish
  issue = issue-selector(pool)            # brief assessment; order is not label order
  run harness "ship" on issue             # sequential; also watches rate_limit_event during the run
  on success: gates passed, reviewers and verifier signed off -> open PR
     merge policy auto:   wait for CI if the repo has it -> squash merge
        CI fails -> ci-fix harness once -> CI passes -> merge; fails again -> blocker
        conflict -> conflict-resolve harness
     merge policy manual: label awaiting-merge and continue with an independent issue
  on failure or block: label blocked, send via human-liaison, continue with independent issues
  at end of group: harness-cleanup (section 10)
```

Usage gate
- Uses `scripts/usage.py`. Defaults: pause at 80% of the 5-hour window and 90% of the weekly window.
- Checked before every harness launch, and inside a run from the `rate_limit_event` in its stream output.
- Pausing is silent. The factory sleeps until `resume_at` and continues without prompting.
- The factory runs as a long-lived process on my Mac under `caffeinate`. If the lid closes it pauses and
  resumes when the machine wakes.

Selector. `issue-selector` reads the `todo` pool, dependencies and the files each issue is likely to touch,
and picks the next issue with a one-line reason logged. It is advisory about order only. It cannot pick
an issue with an unmerged dependency.

## 10. Out-of-scope work

During a run, agents record out-of-scope concerns in the run ledger and never create issues mid-feature.
After PR creation or merge, `harness-cleanup` re-evaluates each concern shallowly: still real, still worth
doing, not a duplicate. Survivors become issues labelled `backlog`, with context linking back to the issue
that raised them. They are never worked until I change the label to `todo`. `harness-cleanup` is the home
for other end-of-run chores later.

## 11. Personal and professional split

This boundary must always hold.

- Professional harnesses keep the current rules exactly: no commits by agents, no pushes, no GitHub writes,
  commits and pushes only by me through the main session as my personal account.
- Factory mode is enabled only by an explicit marker set by the factory launcher, and only for the project
  directory it was started on. The marker is an env var plus a file, and both must agree:
  the launcher exports `SOFTWARE_FACTORY_MARKER=<32-char nonce>` and writes
  `~/.software-factory/markers/<nonce>.json` with `{project, pid, started, role, pr_branch?, issue_branch?, base_branch?}`
  (mode 0600, deleted on exit). The guard accepts it only if the nonce is well-formed, the file is a regular
  file owned by the user and not group/world-writable, the pid is alive, `started` is under 24 hours old, and the
  realpath of the cwd (including `cd` and `git -C` targets) is inside `project`. Any error means no factory mode.
- The role of a subagent always comes from the hook payload's `agent_type`, never from the marker, because
  subagents inherit the env var. A call with no `agent_type` is the manager only if the file says
  `role = "manager"`, so an interactive session in the same terminal gets nothing.
- Factory mode only lifts the subagent denial, and only for: `engineer` add/commit on `issue_branch` (no
  `add -A`/`.`, no `--no-verify`); `ci-fixer` and `conflict-resolver` `git push [origin [<branch>]]` of
  `pr_branch` (no flags, refspecs or URL remotes). `conflict-resolver` also merges `origin/<base_branch>` (or
  `--abort`) into `pr_branch` and may add/commit there, so it never rebases or force-pushes. It never skips the account check (a work-account session is
  still denied), and never lifts force/delete pushes, protected branches, `--amend`, rebase, token/host overrides
  or gh account switching. The manager's own powers are unchanged: they already come from the main-session rules.
- The launcher is `harness factory launch [--role manager|engineer|ci-fixer|conflict-resolver] [--issue-branch B]
  [--pr-branch B] [--base-branch B] [-- <command...>]` (default command: `claude`). It refuses professional
  harnesses and roles missing their branch fields, writes the marker with this process's pid, runs the command in
  the project root with the nonce exported, forwards signals, and deletes the marker on exit. Every launch first
  prunes stale markers; `harness factory cleanup` does only that.
- Workers that need a branch (`engineer`, `ci-fixer`, `conflict-resolver`) run as their own sessions, launched by
  the manager with `harness factory launch --role <role> --<branch flags> -- claude -p ...`, so each gets its own
  marker with its branch fields; Task subagents can't carry per-worker markers. A session whose marker role is not
  `manager` is held to that role's rules like a subagent (it has no `agent_type`, so the guard uses the marker
  role), even outside the project. `ci-fixer` may also add and commit on its `pr_branch`, since its fix has to be
  committed before it pushes.
- Subagent detection uses `agent_id`, not `agent_type`. Checked against Claude Code 2.1.311: a main session has
  neither field, a Task subagent (including plugin ones like `harness:scout`) has both, and a main session started
  with `--agent` has `agent_type` only. Keying on `agent_type` would have treated a `--agent` manager as a subagent.
  Nested subagents were not tested.
- In factory mode the guard denies Bash, Write, Edit and notebook calls that mention or reach `~/.software-factory`.
  That is best effort against a process running as the same user. The real barrier is a manual one-time install
  in `~/.claude/settings.json` (user settings; the plugin can't ship it):
  `"permissions": {"deny": ["Edit(~/.software-factory/**)", "Write(~/.software-factory/**)",
  "NotebookEdit(~/.software-factory/**)", "Bash(*.software-factory*)"]}`. It also blocks harmless commands that
  mention the folder (like `ls`) in Claude sessions; run those from a separate terminal. The launcher is a separate
  process and is unaffected. Check with `/permissions`, then try a write into the folder.
- In factory mode a subagent may not use `git -c`, `--git-dir` or `--work-tree`, or write `git config`, since those
  can redirect a push to another repo or URL.
- The guard defaults to deny. Without the marker, or outside that directory, behaviour is today's.
- Factory-mode permissions are per role (section 4), not blanket. Merges and PR creation belong to the
  factory manager.
- Git writes in factory mode use the personal GitHub account, as the harness does today. Repos have no
  branch protection yet and little CI, so this is fine initially.
- A professional recipe has `mode = "professional"` and cannot be started by the factory.

The guard change is the riskiest part of this design. It gets its own tests, including: marker absent,
marker with the wrong directory, and a subagent without push rights.

## 12. Build order

1. Rename the repo and fix paths (section 14).
2. Factory-mode guard with tests. Nothing else is safe without it.
3. `notify()` with the terminal channel, and `human-liaison` with the standard format.
4. `factory-planner` and issue creation from an MVP doc, with my approval of the issue list.
5. Run loop with `issue-selector`, the ship recipe, manual merge, and the usage gate with pause and resume.
6. Auto-merge, CI wait, `ci-fix` recipe.
7. `question-resolver`, `conflict-resolve`, `harness-cleanup`.
8. Push notification channel.
9. First real project: callback.

## 13. Not in v1

- Parallel harnesses or swarms.
- Stacked branches (an issue building on an unmerged PR).
- Several projects in one factory process.
- A dashboard or web UI.
- Push notifications (scaffolding only).
- Smart blocker summarising beyond the standard format.
- Retrying failed harness runs.

## 14. Rename (done 2026/11/06)

The repo was renamed from `claude-harness` to `software-factory` (GitHub repo, remote URL, folder
`~/code/software-factory`). Places that held the old name or path were updated: `~/.claude/settings.json`
(marketplace path, guard hook, statusline), the plugin registry, `~/.zshrc`, the personal account config
in `~/.claude-personal`, the README and `scripts/claude-accounts.sh`.

- The plugin name stays `harness`, so `/ship` and the `harness:` agent prefix are unchanged.
- Session history under the old folder name does not carry over.
- Issues and PRs created before the rename keep working through GitHub's redirect.

## 15. Open items

- Phone push notifications do not arrive (section 7, "Phone access"). Revisit when fixed.
- Wire the launcher into the run loop: the manager session and the per-role worker sessions still need to be started with `harness factory launch`.
- `ci-fixer` is meant to comment on the PR, but `gh pr comment` is blocked for subagents and worker sessions.
- Confirm what the usage probe reports when I am near or over a limit (the `status` field values are unseen).
