# Claude account switching for harness projects (zsh or bash). Add this line to ~/.zshrc:
#
#   source ~/code/claude-harness/scripts/claude-accounts.sh
#
# Then `claude` started inside a project whose .claude/harness.toml says
#   [accounts]
#   claude = "personal"
# runs with that account's config directory (set in scripts/defaults.toml under [accounts.dirs]).
# Each directory keeps its own login, so both accounts stay signed in. Outside harness projects,
# or when you set CLAUDE_CONFIG_DIR yourself, `claude` behaves exactly as before.
#
# Also adds `claude-as <account>`, `claude-work` and `claude-personal` to pick one explicitly.

if [ -n "${ZSH_VERSION:-}" ]; then
  eval '_HARNESS_HOME="${${(%):-%x}:A:h:h}"'
else
  _HARNESS_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

_harness_run_claude() {  # $1 = "name|dir" ; rest = args
  local spec="$1"; shift
  local name="${spec%%|*}" dir="${spec#*|}"
  if [ -n "$dir" ]; then
    mkdir -p "$dir" && chmod 700 "$dir" 2>/dev/null
    printf 'claude: using the %s account (%s)\n' "$name" "$dir" >&2
    CLAUDE_CONFIG_DIR="$dir" command claude "$@"
  else
    printf 'claude: using the %s account (~/.claude)\n' "$name" >&2
    env -u CLAUDE_CONFIG_DIR claude "$@"
  fi
}

claude() {
  if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then
    local spec
    spec="$(python3 "$_HARNESS_HOME/scripts/harness.py" account 2>/dev/null)"
    if [ -n "$spec" ]; then
      _harness_run_claude "$spec" "$@"
      return
    fi
  fi
  command claude "$@"
}

claude-as() {
  local spec
  spec="$(python3 "$_HARNESS_HOME/scripts/harness.py" account --dir-for "$1")" || return 1
  shift
  _harness_run_claude "$spec" "$@"
}
claude-work() { claude-as work "$@"; }
claude-personal() { claude-as personal "$@"; }
