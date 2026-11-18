#!/usr/bin/env bash
#
# PreToolUse hook (Bash): note a `git commit` that takes whatever happens to be
# staged. `.git/index` is one file shared by every agent working in this tree, so
# `git add` there is a read-modify-write race with a commit as the payload — one
# agent stages, another commits, and the second one's history carries the first
# one's half-finished work. Naming the paths, or building the commit in a private
# index (`GIT_INDEX_FILE` + `commit-tree`, see /gene step 2), avoids that. This
# advises and lets the commit through; a mixed commit is sorted out later, on a
# branch that always converges. Wired globally by install.sh.
#
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=hooks/lib/bash_command.sh
. "$REPO_DIR/lib/bash_command.sh"
# shellcheck source=hooks/lib/advice.sh
. "$REPO_DIR/lib/advice.sh"

CMD="$(jq -r '.tool_input.command // empty' | strip_heredoc_bodies)"

# A commit message is text, not a command: `commit-mine -m "a plain git commit
# would undo it"` runs no git commit. Drop quoted -m/--message bodies (short
# clusters such as -am, and -m"x" with no space, included). Newlines become a
# marker first, so a message can span them while separate lines stay separate.
unmessage() {
  local rs; rs="$(printf '\036')"   # BSD sed has no \x escapes, so splice the byte in
  tr '\n' '\036' \
    | sed -E "s/(^|[[:space:]$rs])(-[a-zA-Z]*m|--message)([[:space:]]+|=)?\"[^\"]*\"/\\1/g; s/(^|[[:space:]$rs])(-[a-zA-Z]*m|--message)([[:space:]]+|=)?'[^']*'/\\1/g" \
    | tr '\036' '\n'
}

# `bash -c "..."` runs its quoted body, so each body is read as a command too,
# whatever the shell's path or the options before -c.
BODIES="$(printf '%s' "$CMD" | tr '\n' ' ' \
  | grep -oE "(^|[[:space:];&|(/\`])(ba|z|da|k|fi)?sh([[:space:]]+-[a-zA-Z]+)*[[:space:]]+-[a-zA-Z]*c([[:space:]]+--)?[[:space:]]+(\"[^\"]*\"|'[^']*')" \
  | sed -E "s/^[^\"']*[\"']//; s/[\"']\$//" || true)"

# One clause per line: every git commit in the command is judged on its own, so a
# scoped one cannot vouch for a sweeping one beside it.
CLAUSES="$( { printf '%s' "$CMD" | unmessage; echo
             printf '%s\n' "$BODIES" | while IFS= read -r body; do printf '%s' "$body" | unmessage; echo; done
           } | tr ';&|' '\n\n\n')"

# Only the porcelain sweeps an index it did not build. `commit-tree` commits a tree
# assembled elsewhere — that is the flow this gate steers toward, so it stays silent.
COMMIT='(^|[[:space:](`])([A-Za-z0-9_./-]*/)?git[[:space:]]+([^[:space:]]*[[:space:]]+)*commit([[:space:]]|$)'
printf '%s\n' "$CLAUSES" | grep -qE "$COMMIT" || exit 0

# A worktree has its own index, and scratch space is a fixture. Neither is shared.
GIT_DIR="$(printf '%s\n' "$CMD" | git_working_dir)"
if [ -n "$GIT_DIR" ] && printf '%s' "$GIT_DIR" | grep -qE "$SCRATCH_PATH"; then
  exit 0
fi

# `-a` stages every tracked change in the tree, which is the whole hazard in one
# flag. `--amend` is a double dash, so a short cluster never matches it.
SWEEPS_ALL='commit([[:space:]]+--?[^[:space:]]+)*[[:space:]]+-[a-zA-Z]*a[a-zA-Z]*([[:space:]]|$)'
# Otherwise the commit is scoped only if a `--` pathspec follows it.
SCOPED='commit([[:space:]]|$).*[[:space:]]--[[:space:]]+[^[:space:]]'

unscoped=0
while IFS= read -r clause; do
  printf '%s' "$clause" | grep -qE "$COMMIT" || continue
  # Any other quoted text is an argument's value, not the argument list.
  args="$(printf '%s' "$clause" | sed -E 's/"[^"]*"//g; s/'"'"'[^'"'"']*'"'"'//g')"
  if printf '%s' "$args" | grep -qE "$SWEEPS_ALL" || ! printf '%s' "$args" | grep -qE "$SCOPED"; then
    unscoped=1
  fi
done <<< "$CLAUSES"

if [ "$unscoped" = 1 ]; then
  advise_tool "Scoped-commit gate: this commit takes whatever is staged in the shared .git/index, which may include another agent's work. Name the paths instead (git commit -m '...' -- path/a path/b), or use the gene skill's scripts/commit-mine for a private-index commit."
fi
