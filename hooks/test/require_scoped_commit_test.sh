#!/usr/bin/env bash
#
# Tests for require_scoped_commit.sh — the shared-index PreToolUse gate.
# Run: hooks/test/require_scoped_commit_test.sh
#
# The gate warns because `.git/index` is one file every agent in a working tree
# shares, so a commit that takes "whatever is staged" takes whatever *they* staged.
# Three ways the warning goes wrong: the plumbing form gets flagged (it is the escape
# hatch the note recommends), a worktree gets flagged (its index is its own), or a
# commit *message* containing " -- " reads as a pathspec and silences a real sweep.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="${SCOPED_HOOK:-$DIR/../require_scoped_commit.sh}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
T="$WORK/t.jsonl"; : > "$T"   # unread: this gate judges the command alone
# shellcheck source=hooks/test/pretooluse_helper.sh
. "$DIR/pretooluse_helper.sh"

echo "== a commit must name what it commits =="
expect_cmd warn  "a bare commit"               "git commit -m x"
expect_cmd warn  "staged, then committed"      "git add a.rb b.rb && git commit -m y"
expect_cmd allow "a pathspec-scoped commit"    "git commit -m x -- a.rb b.rb"
expect_cmd allow "a message with real spaces"  'git commit -m "Add the thing" -- app/a.rb'

echo "== -a sweeps the tree, pathspec or not =="
expect_cmd warn  "the -a flag"                 "git commit -a -m x"
expect_cmd warn  "the -am cluster"             'git commit -am "quick"'
expect_cmd warn  "--all spelled out"           "git commit --all -m x"
expect_cmd warn  "-a with a pathspec anyway"   "git commit -a -m x -- a.rb"

echo "== an amend commits the index too =="
expect_cmd warn  "a bare amend"                "git commit --amend --no-edit"
expect_cmd allow "a scoped amend"              "git commit --amend --no-edit -- a.rb"

echo "== the plumbing form is the recommended escape hatch =="
expect_cmd allow "commit-tree"                 'git commit-tree $tree -p $base -F /tmp/msg.txt'
expect_cmd allow "the ref move"                "git update-ref HEAD abc123 def456"
expect_cmd allow "a private-index add"         'GIT_INDEX_FILE=.git/gene-index git add -- a.rb'
expect_cmd allow "commit-mine, the sanctioned path" "scripts/commit-mine -m x"
expect_cmd allow "a message that mentions git commit" 'scripts/commit-mine -m "a plain git commit would undo it"'
expect_cmd allow "a multi-line message mentioning it" "scripts/commit-mine -m 'Subject

Run git commit -a and it sweeps.'"
expect_cmd allow "a --message= mentioning it"  'scripts/commit-mine --message="git commit -a hazard"'
expect_cmd warn  "a commit whose message names git commit" 'git commit -m "explain git commit"'
expect_cmd warn  "an option ending in m is not -m" 'foo --param "x; git commit -a"'

echo "== a shell -c body is a command =="
expect_cmd warn  "bash -c with a bare commit"  'bash -c "git commit -m x"'
expect_cmd warn  "sh -lc single-quoted sweep"  "sh -lc 'git commit -am quick'"
expect_cmd allow "bash -c with a scoped commit" 'bash -c "git commit -m x -- a.rb"'
expect_cmd allow "bash -c without a commit"    'bash -c "git status"'
expect_cmd allow "bash -c, scoped after a clause" 'bash -c "echo hi; git commit -m x -- a.rb"'
expect_cmd warn  "a path-prefixed shell"       '/bin/bash -c "git commit -m x"'
expect_cmd warn  "options before -c"           'bash -e -c "git commit -m x"'
expect_cmd warn  "the sweep is not the last -c" 'bash -c "git commit -a"; bash -c "git status"'

echo "== each commit is judged on its own =="
expect_cmd warn  "a scoped -c beside a bare commit" 'git commit -m x; bash -c "git commit -m y -- a.rb"'
expect_cmd warn  "scoped then bare"            'git commit -m x -- a.rb && git commit -m y'
expect_cmd allow "two scoped commits"          'git commit -m x -- a.rb && git commit -m y -- b.rb'
expect_cmd warn  "a dangling -- names nothing" 'git commit -m x --'
expect_cmd allow "-m with no space before the quote" 'git commit -m"a;b" -- a.rb'
expect_cmd warn  "a scoped line does not vouch for the next" 'git commit -m x -- a.rb
git commit -m y'
expect_cmd warn  "a commit in a subshell"      '(git commit -m y)'
expect_cmd warn  "a commit in backticks"       'echo `git commit -m y`'
expect_cmd warn  "sh -c -- body"               'sh -c -- "git commit -a"'
expect_cmd allow "unrelated git"               "git status"
# Command-shaped text is treated as a command, as in the sibling gate: a heredoc
# body is the way to quote one, and nothing else needs to.
expect_cmd allow "the bare word in an echo"    "echo commit"
expect_cmd warn  "an echo spelling it out"     "echo git commit -m x"

echo "== an index that is not shared is not the hazard =="
expect_cmd allow "a commit in mktemp"          'cd $(mktemp -d) && git commit -m init'
expect_cmd allow "a /mr-frond worktree"        "cd /tmp/mati/add-avatars && git commit -m x"
expect_cmd allow "an explicit git -C fixture"  "git -C /tmp/fixture commit -m probe"

echo "== a message is not an argument list =="
expect_cmd warn  "a message containing --"     'git commit -m "drop the -- separator"'
expect_cmd warn  "a message naming a path"     'git commit -m "fix a.rb -- properly"'
expect_cmd warn  "a message mentioning /tmp/"  'git commit -m "handle /tmp/ paths"'
expect_cmd allow "a scoped multi-line message"  'git commit -q -m "Subject

Body line.

Co-Authored-By: X <x@example.com>" -- README.md'
expect_cmd allow "a scoped heredoc message"    "git commit -m \"\$(cat <<'EOF'
Subject

Body
EOF
)\" -- a.rb"
expect_cmd warn  "a multi-line message with --" 'git commit -m "Subject

drop the -- separator"'
expect_cmd allow "a heredoc body quoting one"  "cat > notes.md <<XEOF
git commit -m sneaky
XEOF"

report
