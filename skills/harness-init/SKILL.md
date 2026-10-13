---
name: harness-init
description: Set up the harness (/ship, /review-pr, metrics) for the project in the current folder - detects repos, base branches and test commands, picks the Claude and GitHub accounts, writes .claude/harness.toml and a CLAUDE.md.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *)
---

# /harness-init

`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

1. **Scan.** Run `H init`. It prints JSON: the git repos found (the folder itself, or its immediate
   subfolders), each repo's GitHub slug, guessed base branch, stack and detected gates, the gh
   accounts you're signed into, the Claude account names, and whether the project is already
   configured. If `already_configured` is true, ask whether to replace the config (`--force`) or stop.
   If no repos were found, tell the user to `git init` (or clone) first and stop.

2. **Ask** with AskUserQuestion, in one call where possible:
   - "Which Claude account should this project use?" with options from `claude_accounts` (e.g. work,
     personal).
   - "Which GitHub account?" with options from `github_accounts` (skip if there's only one, or none).
     Only accounts in `[github] write_accounts` (the personal one by default) can commit, push or edit
     PRs/issues; any other account can only create PRs and issues, so say so when they pick one.
   - "Git mode?" with "Leave changes uncommitted (I review and commit)" and "Commit on the feature
     branch (one commit per task)".
   - Confirm the base branch if the guesses differ between repos or look wrong (e.g. `main` vs `dev`).

3. **Write.** `H init --write --account <a> --github-user <g> --git-mode <uncommitted|commit>
   [--base <branch>] [--name "<project name>"] [--description "<one line>"]`. It writes
   `.claude/harness.toml`, enables the plugin in `.claude/settings.json`, creates `CLAUDE.md` if there
   isn't one, and keeps `.harness/` out of git.

4. **Describe the project.** This is what makes the agents good in this codebase. Look at the repos
   briefly (README, package.json/pyproject, top-level folders, existing CLAUDE.md files; read
   excerpts, not everything) and:
   - fill in the TODO sections of `CLAUDE.md`: what the project is, how to run/test each part, the
     conventions and rules that matter. Keep it under ~60 lines. If a CLAUDE.md already existed, only
     add what's missing, plus a short "Harness" section if absent.
   - **Required:** edit `.claude/harness.toml` with your Edit tool: replace the generated
     `description = "..."` under `[project]` with one or two sentences on what the project is and its
     stack, and replace `review_focus = []` with 2–6 short, specific checks a reviewer should make in
     this codebase, e.g. `review_focus = ["every API view sets an explicit permission class",
     "money values are rounded to 2 decimals"]`. The reviewers get these in every review.
   - check the detected `gates` make sense (right commands and folders); fix obvious mistakes.

5. **Check.** Run `H doctor` and relay anything marked `!!`.

6. **Tell the user**, briefly:
   - what was configured (repos, base branch, gates, accounts, git mode) and that they can edit
     `.claude/harness.toml` any time;
   - if the account is not the one this session runs on, to quit and restart `claude` from this
     folder (the claude shell function picks the account), and on first use of a new account to run
     `/login`;
   - to restart Claude Code in this folder and accept the trust prompt so the project's settings
     load; then `/ship <issue or task>` and `/review-pr <PR>` are ready.
