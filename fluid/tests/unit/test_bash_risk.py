"""Bash read-only classifier + permission denial messages.

Covers the ``bash_risk`` table (safe vs write), the engine-level behavior
(read-only Bash commands run under ``on_write`` without a prompt), and the
three denial-text variants: denied at the prompt, denied by a configured rule,
and auto-denied because the run cannot prompt.
"""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from fluid.cli.repl import auto_deny
from fluid.engine.native import NativeLoop
from fluid.events import (
    PermissionRequest,
    RespondPermission,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine, Verdict
from fluid.permissions.rules import Rule
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.bash import SPEC as BASH_SPEC
from fluid.tools.builtins.bash_risk import classify, is_read_only
from fluid.tools.registry import ToolCtx, ToolRegistry

# ─── classifier table ────────────────────────────────────────────────────────

SAFE_COMMANDS = [
    "ls",
    "ls -la",
    "dir",
    "pwd",
    "cat file.txt",
    'cat "a b.txt"',
    r"cat C:\Users\dev\notes.txt",  # Windows path as an argument
    "git status --short",
    "git status",
    "git log --oneline -10",
    "git -C subdir log --oneline -5",
    "git --no-pager diff",
    "git diff HEAD~1",
    "git show abc123",
    "git rev-parse HEAD",
    "git ls-files",
    "git blame file.py",
    "git branch",
    "git branch -a",
    "git branch --list",
    "git remote -v",
    "git stash list",
    "git tag -l",
    "git describe",
    "git config --get user.name",
    "git config --list",
    "cat a | grep b",
    "cat a | grep b | wc -l",
    "grep -n 'permission denied' fluid/app.py | head -20",
    "sed -n 1,5p file.txt",
    "sed -n '1,5p' file.txt",
    "awk '{print $1}' data.txt",
    "find . -name '*.py'",
    "find . -name x -type f",
    "wc -l file.txt",
    "head -n 20 file.txt",
    "tail -5 log.txt",
    "sort file.txt | uniq",
    "cut -d, -f1 data.csv | tr a-z A-Z",
    "cmp a.txt b.txt",
    "stat file.txt",
    "du -sh .",
    "df -h",
    "echo hello world",
    "printf '%s\\n' hi",
    "which git",
    "where git",
    "whoami && uname -a",
    "date",
    "printenv PATH",
    "env",
    "jq '.name' package.json",
    "tree -L 2",
    "basename /a/b/c.txt",
    "realpath .",
    "readlink some-link",
    "file archive.zip",
    "true",
    "uv --version",
    "uv pip list",
    "pip list",
    "npm list",
    "npm outdated",
    "cargo tree",
    "go version",
    "node --version",
    "ls 2>&1",
    "ls 2>&1 | head -5",
    "LANG=C grep foo bar",  # locale assignments cannot change what grep does
    "LC_ALL=C sort file.txt",
    "NO_COLOR=1 ls",
    "sed -n '/error/p' file.txt",  # an e inside an address regex is not the e command
    "sed -n 's/one/two/p' file.txt",
    "grep 'foo.*bar' file.txt",  # quoted regex, not a glob
    "grep -n 'a.*' src/app.py",
    "grep foo *.py",  # glob that cannot reach a credential file
    "ls *.log",
]

WRITE_COMMANDS = [
    "",
    "   ",
    "rm -rf x",
    "git push",
    "git push origin main",
    "git commit -m 'x'",
    "git checkout -b feature",
    "git branch -D old",  # branch beyond listing flags
    "git stash pop",
    "git stash",
    "git config user.name x",  # config write form
    "git config --unset user.name",
    "git -c core.pager=evil log",  # -c can point git at external programs
    "git log --output=out.txt",  # --output writes a file
    "ls > out.txt",
    "ls >> out.txt",
    "echo hi > file",
    "cat a 2> err.txt",
    "cat <(ls)",  # process substitution
    "cat a | xargs rm",
    "cat a | xargs -n1 echo",  # xargs is never safe
    r"find . -name x -exec rm {} \;",
    "find . -name x -execdir rm {} +",
    "find . -name '*.tmp' -delete",
    "find . -ok rm {} +",
    "sed -i s/a/b/ file",
    "sed --in-place s/a/b/ file",
    "sed s/a/b/ file",  # without -n sed prints/could do more; conservative
    "sed -n 'w out.txt' file",  # sed w command writes a file
    "awk 'BEGIN{system(\"rm x\")}'",
    "awk '{print $1 | \"sort\"}' f",  # awk piping to a command
    "echo `ls`",  # backticks
    "echo $(ls)",  # command substitution
    "sudo ls",
    "python -c 'print(1)'",
    "python3 script.py",
    "node app.js",
    "npm install",
    "npm run build",
    "cargo build",
    "uv sync",
    "pip install requests",
    "go build ./...",
    "touch marker.txt",
    "mkdir newdir",
    "cp a b",
    "mv a b",
    "curl https://example.com",
    "env rm x",  # env with arguments runs a command
    "sort -o out.txt file",  # sort -o writes
    "sort --output=x a.txt",
    "uniq input output",  # second positional is an output file
    "date -s '2020-01-01'",  # sets the clock
    "rg --pre cmd pattern",  # rg --pre runs a command
    "rg --pre=cmd foo",
    "ls &",  # background job
    "(ls)",  # subshell
    "ls; rm x",  # one bad simple command poisons the whole line
    "true && rm x",
    "cat a || rm b",
    "ls\nrm -rf x",  # newline as a command separator
    'ls "unterminated',  # unlexable → conservative write
    "FOO=1",  # bare assignment is not a read
    "unknowntool --help",
    # Environment variables that make a read-only program run something else.
    "FOO=bar ls",  # not on the inert allowlist
    "GIT_EXTERNAL_DIFF=/tmp/evil.sh git diff",
    "LD_PRELOAD=/tmp/e.so ls",
    "GIT_PAGER='sh -c \"rm -rf x\"' git log",
    "LESSOPEN='|rm %s' less foo",
    "BASH_ENV=/tmp/x ls",
    "less foo",  # LESSOPEN/LESSCLOSE run commands, so less is off the table
    # GNU sed e runs a shell command; -f/awk -f load a script we never see.
    "sed -n '1e rm -rf x' f",
    "sed -n 's/a/b/e' f",
    "sed -n 's/a/b/ge' f",
    "sed -n -e '1e rm' f",
    "sed -n --expression='1e rm' f",
    "sed -n -f script.sed f",
    "sed -nf script.sed f",
    "sed -n --file=script.sed f",
    "awk -f prog.awk f",
    "awk --file=prog.awk f",
    "gawk -l ext f",  # loads an extension library
    "gawk -i inc.awk f",
]


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_read_only_commands_are_safe(command: str) -> None:
    assert is_read_only(command), command
    assert classify({"command": command}) == "safe"


@pytest.mark.parametrize("command", WRITE_COMMANDS)
def test_everything_else_is_write(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


@pytest.mark.parametrize(
    "command",
    [
        "cat .env*",
        "cat .env?",
        "cat ?env",
        "cat .en[v]",
        "cat config/.env*",
        "cat ~/.ssh/*",
        "head /root/.aws/*",
        "cat *",  # could expand to anything, .env included
        "cat *.pem",
        "cat '.en'*",  # the unquoted tail is still a glob
        "cat {.env",  # unbalanced brace: unresolvable, so not safe
    ],
)
def test_globs_that_could_reach_a_credential_file_are_write(command: str) -> None:
    assert is_read_only(command), command  # read-only program, but ...
    assert classify({"command": command}) == "write"


# Banned options in spellings the exact-token check missed, and awk program
# forms that run or write without the literal ``system(``. Each was "safe"
# before; a false "ask" is acceptable here, a false "safe" is not.
ESCAPING_SPELLINGS = [
    "awk 'BEGIN{system (\"rm x\")}'",
    "awk 'BEGIN{f=\"sys\" \"tem\"; @f(\"rm x\")}'",  # gawk indirect call
    "gawk '@include \"inplace\"; {print}' f",
    "gawk '@load \"rwarray\"; BEGIN{}'",
    "awk -Pf prog.awk f",  # -f bundled after another flag
    "awk --fil=prog.awk f",  # abbreviated --file
    "gawk -E prog.awk f",
    "gawk --exec=prog.awk f",
    "gawk -d 'BEGIN{}'",  # writes awkvars.out
    "gawk --dump-variables=out 'BEGIN{}'",
    "gawk -p '{print}' f",  # writes awkprof.out
    "gawk -o '{print}' f",
    "mawk -W exec prog.awk",
    "sort -o/tmp/x a.txt",
    "sort -uo/tmp/x a.txt",
    "sort -uo /tmp/x a.txt",
    "sort -nuo /tmp/x a.txt",
    "sort --outp=x a.txt",
    "sort --compress-program=./x a.txt",
    "tree -o out.txt",
    "tree -ao out.txt",
    "tree -R -H . -L 1",
    "date -s2020-01-01",
    "date -us 2020-01-01",
    "date --se=2020-01-01",
    "rg --hostname-bin=./x --hyperlink-format=default foo",
    "uniq - out.txt",
    "uniq -- -in out.txt",
    "sed -n '-ew out' f",
    "sed -n -e'w out' f",
    "sed -n --i 's/a/b/p' f",  # abbreviated --in-place
    "sed -n --fi=script.sed f",  # abbreviated --file
    "file -C -m magic",  # compiles magic.mgc
    "file --compile -m magic",
    "file --co -m magic",  # unambiguous abbreviation of --compile
    "tree -- -o out.txt",  # tree's own parser: -- is not trusted to end options
    # awk program text is lexed: calls and @-constructs ask, strings and
    # regex literals do not hide them.
    "awk 'BEGIN{@f(\"x\")}'",
    "gawk '@namespace \"x\"; BEGIN{}'",
    "awk '{print}' -e '@f()'",  # an option after the program (permuting awk)
    "awk -e 'BEGIN{x=\"' -e '\"; @f()}'",  # a string split across -e values
    "awk 'BEGIN{if (x) /@/}'",  # regex or division: not settled, so ask
    # sed: w/W need no particular filename characters; r reads a file the
    # credential check never sees; unparsed regex delimiters ask.
    "sed -n 'W@x' f",
    "sed -n 's/a/b/w@out' f",
    "sed -n 'w$x' f",
    "sed -n '1r x' f",
    "sed -n 's/[/]/x/p' f",  # GNU and BSD sed end this regex differently
    # date: only display forms; a bare operand sets the clock.
    "date 01011200",
    "date +%s 01011200",
    # sed -l takes a value in GNU sed and none in BSD/macOS sed.
    "sed -n -l 5 p f",
    "sed -nl5 p f",
]

# Everyday read-only forms around the same programs stay "safe".
STILL_SAFE = [
    "sort -u file",
    "sort -n -k2 file",
    "sort -rn file",
    "sort -t, -k3 -rn file",
    "sort --parallel=2 file",
    "awk '{print $1}' f",
    "awk -F: '{print $1}' f",
    "awk -F@ '{print $2}' f",
    "awk -vline=3 'NR==line' f",
    "awk -v n=1 '{print $n}' f",
    "awk 'NR<5' f",
    "tree -L 2",
    "tree -a -I node_modules",
    "tree -d --noreport",
    "date -u +%s",
    "date -Iseconds",
    "date -d yesterday +%F",
    "date --iso-8601=seconds",
    "uniq -c f",
    "uniq -",
    "sed -ne '/x/p' f",
    "sed -n -e '1,3p' f",
    "file -b x",
    # Only awk's program text is inspected, and only for real calls.
    "awk -F @ '{print $2}' f",
    "awk '/@example\\.com$/' f",
    "awk '$2 ~ /@example\\.com$/ {print $1}' f",
    "awk '{system_name=$1; print system_name}' f",
    "awk '{print $1}' user@host.log",
    "awk 'BEGIN{x=\"a@b\"; print x}'",
    "awk '{print ($1+$2)/2}' f",
    "awk '{gsub(/foo/, \"bar\"); print}' f",
    "awk '!seen[$0]++' f",
    # An ambiguous abbreviation sets nothing.
    "sort --c file",  # --check or --compress-program: sort refuses it
    "sed -n '$p' f",
    "sed -n '/a/,/b/{p}' f",
    "sed -n '/[0-9]/p' f",
    "sed -n '1p;$p' f",
    "sed -n 's|/usr|/opt|p' f",
    "date -R",
    "date --rfc-3339=seconds",
    "date -r f +%F",
    "date -d @1700000000",
    "date --help",
    "date --version",
    "date --resolution",
    # s with # or ; as its delimiter (handy when the pattern has slashes).
    "sed -n 's#a/b#c#p' f",
    "sed -n 's;a/b;c;gp' f",
    "sed -n 's#/usr#/opt#p;$p' f",
]


@pytest.mark.parametrize("command", ESCAPING_SPELLINGS)
def test_banned_options_in_any_spelling_are_write(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


@pytest.mark.parametrize("command", STILL_SAFE)
def test_common_read_only_forms_stay_safe(command: str) -> None:
    assert is_read_only(command), command
    assert classify({"command": command}) == "safe"


# A ``--`` that is an option's value is not end-of-options: the program keeps
# reading options after it. Every value-taking option spelling, given ``--``
# as its value, followed by every spelling of a banned option, must ask.
_SORT_VALUE_SPELLINGS = ["-k", "-nk", "-t", "-S", "--key", "--ke", "--field-separator"]
_SORT_BANNED_SPELLINGS = ["--outp=x", "--compress=x", "-ox", "-uox", "-uo x"]
DASH_DASH_AS_VALUE = [
    f"sort {value} -- {banned} f"
    for value in _SORT_VALUE_SPELLINGS for banned in _SORT_BANNED_SPELLINGS
] + [
    "awk -v -- --fil=x f",
    "awk -F -- --exe=x f",
    "awk --assign -- --dump=x '{print}' f",
    "sed -n -e -- --fi=x f",
    "sed -n --expression -- --in=x f",
    "date -d -- -s2020",
    "date --date -- --se=2020",
    "date -r -- -us 2020",
]


@pytest.mark.parametrize("command", DASH_DASH_AS_VALUE)
def test_dash_dash_as_an_option_value_does_not_end_options(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


# Every sort word is scanned, ``--`` included: which word an option takes as
# its value can differ (obsolete options take the next word only when it is
# a number), so no end-of-options marker is trusted.
_SORT_OBSOLETE_SPELLINGS = ["-y", "-ny"]
SORT_UNCERTAIN_VALUE = [
    f"sort {obsolete} {value} -- {banned} f"
    for obsolete in _SORT_OBSOLETE_SPELLINGS
    for value in ["-k", "-t", "-S", "--key"]
    for banned in _SORT_BANNED_SPELLINGS
] + ["sort -- -old.txt", "sort -k2 -- -o x f"]


@pytest.mark.parametrize("command", SORT_UNCERTAIN_VALUE)
def test_sort_scans_every_word(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


# Which bundled sort letters take a value differs between sorts, so a banned
# letter anywhere in a short-option word counts, after a value-taking letter
# or inside what one sort would read as its value. -T and its long name pick
# the directory sort writes temporary files to.
SORT_BUNDLED_OR_TEMP_DIR = [
    f"sort -{lead}{banned} f"
    for lead in ["S", "nS", "k", "t", "y", "S1"]
    for banned in ["o x", "ox", "T x", "Tx", "uo x"]
] + [
    "sort -T x f", "sort -Tx f", "sort -uT x f",
    "sort --temporary-directory=x f", "sort --temporary-directory x f",
    "sort --temp x f", "sort --te=x f", "sort --t x f",
]


@pytest.mark.parametrize("command", SORT_BUNDLED_OR_TEMP_DIR)
def test_sort_banned_letters_anywhere_and_temp_dir_ask(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


@pytest.mark.parametrize("command", [
    "sort -S 1M file", "sort -S1M -n file", "sort --buffer-size=1M file",
    "sort -t: -k2,2n file", "sort -k1,1 -k2nr file", "sort file",
])
def test_sort_default_temp_spill_and_values_stay_safe(command: str) -> None:
    assert is_read_only(command), command


# awk may carry only POSIX -F/-v (attached or separate) and --. Any other
# option asks: awks differ on which options exist and on what they do with
# one they do not know, so the word they run as the program can differ from
# the one the classifier lexed. Each case is an otherwise harmless program.
_AWK_NON_POSIX_OPTIONS = [
    "-e '{print}'", "-e'{print}'", "--source '{print}'", "--source='{print}'",
    "--exec x", "-b", "-c", "-C", "-P", "-O", "-M", "-S", "-n", "-r", "-t", "-k",
    "-Lfatal", "-L", "--sandbox", "--csv", "--posix", "--re-interval",
    "--traditional", "--lint", "--version", "-W version", "-Wv", "-vx=1 -b",
]
AWK_NON_POSIX = [f"awk {opt} '{{print $1}}' f" for opt in _AWK_NON_POSIX_OPTIONS] + [
    "awk -e'{print}' '@f()'",  # a later word that another awk would run
    "awk -- -W '{print}' f",  # an option-looking program
    "awk -F, -- -d f",
]


@pytest.mark.parametrize("command", AWK_NON_POSIX)
def test_awk_asks_on_options_beyond_posix(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


@pytest.mark.parametrize("command", [
    "awk -F, '{print $1}' f",
    "awk -F , '{print $1}' f",
    "awk -v n=2 -F: '{print $n}' f",
    "awk -vn=2 '{print $n}' f",
    "awk -- '{print $1}' f",
])
def test_awk_posix_options_stay_safe(command: str) -> None:
    assert is_read_only(command), command


# A process's environment holds the host's provider keys: Bash runs under
# ``sh -c`` from the host, so ``$PPID`` is the host process.
PROCESS_SECRET_READS = [
    "cat /proc/$PPID/environ",
    "cat /proc/self/environ",
    "head -c 100 /proc/$PPID/environ",
    "cat /proc/thread-self/environ",
    'cat "/proc/$PPID/environ"',
    "cat '/proc/1/environ'",
    "cat /proc/*/environ",
    "cat /proc/self/e*",
    "cat /proc/1/{environ,status}",
    "cat /proc/1/$F",
    "cat /proc/self/task/12/environ",
    "cat /proc/self/../1/environ",
    "cat /dev/fd/../environ",  # /dev/fd is /proc/self/fd
    "cat /proc/self/cmdline",
    "tail -c 64 /proc/1/mem",
    "tr '\\0' '\\n' < /proc/self/environ",
    "sort /proc/1/ENVIRON",
    "date --file=/proc/self/environ",  # a value attached to an option
    "grep -f/proc/self/environ x.py",
]


@pytest.mark.parametrize("command", PROCESS_SECRET_READS)
def test_process_environment_reads_ask(command: str) -> None:
    assert classify({"command": command}) == "write"


def test_relative_reads_are_checked_against_cwd() -> None:
    assert classify({"command": "cat environ", "cwd": "/proc/1"}) == "write"
    assert classify({"command": "cat config", "cwd": "/root/.ssh"}) == "write"
    assert classify({"command": "cat README.md", "cwd": "/repo"}) == "safe"


# Searches and comparisons across a tree print any credential file in it, which
# the Grep tool would withhold.
TREE_READS = [
    "grep -r API_KEY .",
    "grep -rn API_KEY",
    "grep -R pattern lib",
    "grep -inr x src",
    "grep -d recurse KEY .",
    "grep -drecurse KEY .",
    "grep --directories=recurse KEY .",
    "grep --dir=recurse KEY .",
    "grep --recursive KEY .",
    "grep --rec KEY .",
    "grep --dereference-recursive KEY .",
    "grep -- -r x",  # every word is scanned
    "egrep -r KEY .",
    "fgrep -R KEY .",
    "cat x | grep -r KEY .",
    "rg API_KEY",  # recursive by default
    "rg foo file.py",  # RIPGREP_CONFIG_PATH options (--pre) apply to any rg
    "rg --no-config -n foo src",
    "rg --pre-glob '*.pdf' foo",
    "diff -rN . /tmp/empty",
    "diff a b",  # two directories compare every file in them
    "git diff --no-index /tmp/empty .",
    "git diff --no-ind /tmp/empty .",
    "git diff ../empty .",  # outside the work tree: an implicit --no-index
    "git diff main feature",
    "git diff -- a b",
    "git diff -- -a -b",  # after -- every word is a path, dashes or not
    "git diff -- a -b",
    "git diff d*",  # one word, two directories once the shell expands it
    "git show HEAD:.env",  # <rev>:<path> names a credential file
]


@pytest.mark.parametrize("command", TREE_READS)
def test_tree_wide_reads_ask(command: str) -> None:
    assert classify({"command": command}) == "write"


def test_grep_asks_when_grep_options_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREP_OPTIONS", "-r")
    assert classify({"command": "grep foo file.py"}) == "write"
    monkeypatch.delenv("GREP_OPTIONS")
    assert classify({"command": "grep foo file.py"}) == "safe"


@pytest.mark.parametrize("command", [
    "file -p x", "file --preserve-date x", "file --pre x", "file -bp x",
])
def test_file_preserve_date_asks(command: str) -> None:
    """libmagic's preserve-date resets the access time: a metadata write (#1808)."""
    assert classify({"command": command}) == "write"


# Words the shell builds after the classifier has looked: each becomes an
# option or file name the checks never saw. ``find . ${X:--exec} rm {} +``
# runs rm under bash.
HIDDEN_EXPANSIONS = [
    "find . ${X:--exec} rm {} +",
    "find . $'-exec' rm {} +",
    'find . $"-delete"',
    "find . {-exec,rm} {} +",
    "echo -exec; find . -o $_ rm {} +",
    "cat ${X:-.env}",
    "sort ${X:--o} out in",
    "uniq a{,.out}",  # one word becomes two: uniq writes its second operand
    "cat .{env,bashrc}",
    "echo ${#X}",
    "echo $1",
    "echo $@",
    "echo $$",
    "echo $BASH_COMMAND",
    # Any parameter outside single quotes: its value is unseen (an option,
    # a path, several words).
    "echo $HOME",
    "echo ${HOME}",
    'ls "$HOME"',
    "echo $PWD",
    'cat "$F"',
    "grep -n x $DIR",
    "$CMD",
    '"$GREP" x f',
    'echo "a${X}b"',
    "echo hi | grep $P",
]


@pytest.mark.parametrize("command", HIDDEN_EXPANSIONS)
def test_hidden_shell_expansions_ask(command: str) -> None:
    assert not is_read_only(command), command
    assert classify({"command": command}) == "write"


STILL_SAFE_AFTER_SECRET_CHECKS = [
    "grep foo file.py",
    "grep -n 'def main' app/main.py",
    "grep -in error log.txt",
    "grep -c mem file.txt",
    "grep -e foo -e bar f.txt",
    "grep -v '^#' config.ini",
    "cat a | grep b",
    "git diff",
    "git diff HEAD~1",
    "git diff --stat",
    "git diff main...HEAD",
    "git diff HEAD~1..HEAD",
    "git diff -- app/main.py",
    "cat /proc/cpuinfo",
    "cat /proc/self/status",
    "head -c 200 /proc/self/maps",
    "grep environ notes.txt",  # a pattern, not a /proc path
    "cat environ",  # a file in the working directory
    "awk '{print ($1+$2)/2}' f",
    "awk '{gsub(/foo/, \"bar\"); print}' f",
    "echo {}",
    "echo a,b",
    "sed -n '$p' f",
    "grep 'foo$' f",
    'grep "foo$" f',  # $ before the closing quote is literal
    "echo 'cost: $HOME'",
    "git diff -- app/main.py",
    'echo "progress/mem"',  # echo prints its words; it opens nothing
    "printf '%s\\n' progress/mem /proc/1/fd/3",
    "file -b x",
    "cat .env.example",
]


@pytest.mark.parametrize("command", STILL_SAFE_AFTER_SECRET_CHECKS)
def test_everyday_reads_stay_safe_after_secret_checks(command: str) -> None:
    assert classify({"command": command}) == "safe"


# A program named by a path is whatever file is there, whatever its basename.
@pytest.mark.parametrize("command", [
    "/usr/bin/grep -n pattern src/app.py",
    "./cat f",
    "bin/ls",
    r'"C:\Program Files\Git\bin\git.exe" status',
    r"C:\Windows\cat x",
])
def test_programs_named_by_a_path_ask(command: str) -> None:
    assert classify({"command": command}) == "write"


# Descriptor and per-process directory links point wherever the reading
# process has them; they ask rather than get resolved.
@pytest.mark.parametrize("command", [
    "cat /proc/self/fd/3",
    "cat /proc/1/task/2/fd/0",
    "cat /dev/fd/3",
    "ls /dev/fd/",
    "ls /proc/self/fd",
    "cat /proc/1/cwd/notes.txt",
    "cat /proc/self/root/home/u/notes.txt",
    "cat /proc/1/fdinfo/3",
    "cat /proc/1/map_files/7f-80",
    "cat /proc/*/cwd/x",
    "cat /proc/1/cwd/../x",
])
def test_descriptor_and_process_directory_paths_ask(command: str) -> None:
    assert classify({"command": command}) == "write"


def test_descriptor_paths_relative_to_cwd_ask() -> None:
    assert classify({"command": "cat fd/3", "cwd": "/proc/1"}) == "write"
    assert classify({"command": "cat cwd/x", "cwd": "/proc/self"}) == "write"


@pytest.fixture
def linked_tree(tmp_path):
    """A working directory with harmless-looking links to credential files."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("TOKEN=x\n")
    (work / "readme.txt").write_text("hi\n")
    (work / "notes.txt").symlink_to(".env")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / ".credential-keys").mkdir()
    (work / "keydir").symlink_to(tmp_path / "keys" / ".credential-keys")
    return work


@pytest.mark.parametrize("command", [
    "cat notes.txt",
    "head -n 3 ./notes.txt",
    "cat no*",
    "cat n?tes.txt",
    "ls keydir",
    "cat keydir/../../work/notes.txt",
    "grep -f notes.txt readme.txt",
    "sort --random-source=notes.txt readme.txt",
])
def test_links_to_credential_files_ask(linked_tree, command: str) -> None:
    assert classify({"command": command, "cwd": str(linked_tree)}) == "write"
    # The session cwd counts when the call has none.
    assert BASH_SPEC.effective_risk({"command": command}, cwd=str(linked_tree)) == "write"
    assert PermissionEngine("on_write").decide(
        BASH_SPEC, {"command": command}, cwd=str(linked_tree)
    ).decision == "ask"


def test_links_resolve_from_a_relative_call_cwd(linked_tree) -> None:
    risk = BASH_SPEC.effective_risk(
        {"command": "cat notes.txt", "cwd": linked_tree.name}, cwd=str(linked_tree.parent)
    )
    assert risk == "write"


def test_files_that_are_not_links_to_secrets_stay_safe(linked_tree) -> None:
    for command in ("cat readme.txt", "cat r*", "ls", "wc -l readme.txt"):
        assert classify({"command": command, "cwd": str(linked_tree)}) == "safe", command
        assert BASH_SPEC.effective_risk({"command": command}, cwd=str(linked_tree)) == "safe"


def test_the_model_cannot_supply_the_session_cwd(linked_tree, tmp_path, monkeypatch) -> None:
    from fluid.tools.registry import SESSION_CWD_KEY

    monkeypatch.chdir(tmp_path)
    # A model-supplied key is dropped: an empty dir gives no link to resolve,
    # so the call is judged from the host's cwd, and the real cwd still wins.
    spoof = {"command": "cat notes.txt", SESSION_CWD_KEY: str(tmp_path / "empty")}
    assert BASH_SPEC.effective_risk(spoof, cwd=str(linked_tree)) == "write"
    seen: list[dict] = []
    probe = type(BASH_SPEC)(
        name="Probe", description="", input_schema={}, risk="write",
        classify_risk=lambda i: seen.append(i) or "safe",
    )
    probe.effective_risk({"command": "x", SESSION_CWD_KEY: "/attacker"})
    assert SESSION_CWD_KEY not in seen[-1]


@pytest.mark.parametrize("command", [
    "echo .env",  # printed text still gets the credential-name check
    "printf x .env*",
    "echo /proc/1/environ",
    "cat x\\;echo .env",  # one word to the shell: cat reads .env
    "echo hi; cat /proc/1/mem",
    "echo hi\ncat /proc/1/mem",
])
def test_text_only_programs_do_not_shield_other_words(command: str) -> None:
    assert classify({"command": command}) == "write"


def test_classify_without_command_is_write() -> None:
    assert classify({}) == "write"
    assert classify({"command": 42}) == "write"


def test_bash_spec_wires_the_classifier() -> None:
    """The static risk stays write; the per-input hook lowers it for reads."""
    assert BASH_SPEC.risk == "write"
    assert BASH_SPEC.effective_risk({"command": "git status --short"}) == "safe"
    assert BASH_SPEC.effective_risk({"command": "rm -rf x"}) == "write"


# ─── engine decisions ────────────────────────────────────────────────────────


def test_on_write_policy_uses_effective_risk() -> None:
    engine = PermissionEngine("on_write")
    assert engine.evaluate(BASH_SPEC, {"command": "git status"}) == "allow"
    assert engine.evaluate(BASH_SPEC, {"command": "ls -la | wc -l"}) == "allow"
    assert engine.evaluate(BASH_SPEC, {"command": "rm -rf x"}) == "ask"
    # always_ask still asks even for read-only commands.
    assert PermissionEngine("always_ask").evaluate(BASH_SPEC, {"command": "ls"}) == "ask"


def test_decide_names_the_rule_that_denied() -> None:
    engine = PermissionEngine(
        "never_ask", rules=[Rule(tool="Bash", decision="deny", input_globs={"command": "rm *"})]
    )
    verdict = engine.decide(BASH_SPEC, {"command": "rm -rf x"})
    assert isinstance(verdict, Verdict)
    assert verdict.decision == "deny" and verdict.source == "rule"
    assert "'Bash'" in verdict.deny_message()
    assert verdict.deny_message().startswith("permission denied by a configured rule")


# ─── engine-level: the native loop ───────────────────────────────────────────


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _bash(tuid: str, command: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": "Bash", "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({"command": command})}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _loop(provider, tmp_path, *, permissions, store=None, sid=None) -> NativeLoop:
    registry = ToolRegistry()
    register_builtins(registry)
    return NativeLoop(
        provider=provider, store=store or MemoryStore(), model="m", session_id=sid,
        registry=registry, ctx=ToolCtx(cwd=str(tmp_path)), permissions=permissions,
    )


async def _watch_first(loop: NativeLoop, sid: str, kind: type):
    holder: dict = {}
    got = asyncio.Event()

    async def watch() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, kind):
                holder["e"] = e
                got.set()
                return

    return holder, got, asyncio.create_task(watch())


async def test_git_status_runs_without_prompt_under_on_write(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "git status --short"), _text("done")])
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"))
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=15)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, PermissionRequest) for e in events)
    assert any(isinstance(e, ToolResult) for e in events)  # the command ran
    assert any(isinstance(e, TurnComplete) for e in events)


async def test_rm_rf_prompts_under_on_write(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "rm -rf x"), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"),
                 store=store, sid=sid)
    holder, got, watcher = await _watch_first(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=5)
    req: PermissionRequest = holder["e"]
    assert req.name == "Bash" and req.risk == "write"
    await loop.submit(RespondPermission(request_id=req.request_id, decision="deny"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await watcher

    result = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult))
    assert result.is_error
    # Variant 1: denied by the user at the prompt (responder gave no reason).
    assert "permission denied by the user at the prompt" in result.output


async def test_rule_denial_names_the_rule(tmp_path) -> None:
    provider = ScriptedProvider([_bash("t1", "rm -rf x"), _text("done")])
    permissions = PermissionEngine("never_ask", rules=[Rule(tool="Bash", decision="deny")])
    loop = _loop(provider, tmp_path, permissions=permissions)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    result = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult))
    assert result.is_error
    # Variant 2: denied by a configured rule, naming its tool pattern.
    assert "configured rule" in result.output and "'Bash'" in result.output


async def test_non_interactive_auto_deny_explains_itself(tmp_path) -> None:
    request = PermissionRequest(
        session_id="s", turn_id="t", request_id="perm_1",
        tool_use_id="tu1", name="Bash", input={"command": "rm -rf x"}, risk="write",
    )
    response = await auto_deny(request)
    # Variant 3: the run cannot prompt; the message says how to unblock.
    assert isinstance(response, RespondPermission)
    assert response.decision == "deny" and response.request_id == "perm_1"
    assert response.message is not None
    assert response.message.startswith("permission denied: this non-interactive run cannot prompt")
    assert "--approval-policy never_ask" in response.message
    assert "Bash" in response.message and ".fluid/config.toml" in response.message
    assert "Do not retry the same action through another tool" in response.message


async def test_doom_loop_forced_prompt_carries_reason(tmp_path) -> None:
    same = _bash("t", "echo hi")
    provider = ScriptedProvider([same, same, same, _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(provider, tmp_path, permissions=PermissionEngine("on_write"),
                 store=store, sid=sid)
    holder, got, watcher = await _watch_first(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=15)
    req: PermissionRequest = holder["e"]
    assert req.reason is not None and "doom-loop guard" in req.reason
    await loop.submit(RespondPermission(request_id=req.request_id, decision="deny"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await watcher

    results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
    # The denial text tells the model both what happened and why it was asked.
    assert results[-1].is_error
    assert "permission denied by the user at the prompt" in results[-1].output
    assert "doom-loop guard" in results[-1].output


# ─── allow_always is scoped to the programs a Bash command runs ──────────────


def test_program_names_collects_every_program() -> None:
    from fluid.tools.builtins.bash_risk import program_names

    assert program_names("cat a | grep b && ls") == frozenset({"cat", "grep", "ls"})
    assert program_names('"C:\\Program Files\\Git\bin\\git.exe" status') == frozenset({"git"})
    assert program_names("FOO=1 git log") == frozenset({"git"})
    assert program_names("echo $(rm x)") is None
    assert program_names("") is None


def test_allow_always_on_bash_covers_same_programs_only() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as bash_spec

    engine = PermissionEngine("on_write")
    push = {"command": "git push origin main"}
    assert engine.decide(bash_spec, push).decision == "ask"
    engine.allow_always(bash_spec.name, bash_spec.scope_of(push))
    assert engine.decide(bash_spec, push).source == "session"
    assert engine.decide(bash_spec, {"command": "git commit -m x"}).source == "session"
    assert engine.decide(bash_spec, {"command": "rm -rf build"}).decision == "ask"
    assert engine.decide(bash_spec, {"command": "git push | tee log"}).decision == "ask"
    # A later unscoped allow widens to every Bash call.
    engine.allow_always(bash_spec.name, None)
    assert engine.decide(bash_spec, {"command": "rm -rf build"}).source == "session"


def test_a_later_scoped_allow_never_narrows_an_unscoped_one() -> None:
    """Once a session allow covers every Bash call, a later scoped
    ``allow_always`` must leave it covering every call (not crash on the
    ``None`` marker, not shrink it to the new programs)."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as bash_spec

    engine = PermissionEngine("on_write")
    engine.allow_always(bash_spec.name, None)
    engine.allow_always(bash_spec.name, bash_spec.scope_of({"command": "git status"}))
    assert engine.decide(bash_spec, {"command": "rm -rf build"}).source == "session"
    assert engine.decide(bash_spec, {"command": "git push"}).source == "session"


def test_allow_always_without_scope_hook_covers_every_call() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.registry import ToolSpec

    spec = ToolSpec(name="Write", description="", input_schema={}, risk="write")
    engine = PermissionEngine("on_write")
    engine.allow_always("Write", spec.scope_of({"file_path": "a"}))
    assert engine.decide(spec, {"file_path": "b"}).source == "session"


# ─── allow_always labels carry what a wrapper or subcommand program runs ─────


def test_scope_labels_name_the_wrapped_program() -> None:
    from fluid.tools.builtins.bash_risk import scope_labels

    assert scope_labels("sudo apt list") == frozenset({"sudo apt"})
    assert scope_labels("sudo rm -rf /") == frozenset({"sudo rm"})
    assert scope_labels("env FOO=1 make test") == frozenset({"env make test"})
    assert scope_labels("find . -name '*.py' | xargs grep TODO") == frozenset(
        {"find", "xargs grep"}
    )
    assert scope_labels("npm run build") == frozenset({"npm run"})
    assert scope_labels("npm install left-pad") == frozenset({"npm install"})
    assert scope_labels("uv run pytest && uv pip install x") == frozenset(
        {"uv run", "uv pip"}
    )
    assert scope_labels("make") == frozenset({"make"})
    assert scope_labels("make clean") == frozenset({"make clean"})
    # Plain programs keep their program-wide label.
    assert scope_labels("git status && git push") == frozenset({"git"})


def test_scope_labels_refuse_wrappers_with_options() -> None:
    from fluid.tools.builtins.bash_risk import scope_labels

    # Which token is the wrapped program is not knowable from the text.
    assert scope_labels("sudo -u root rm -rf /") is None
    assert scope_labels("echo $(rm x)") is None


def test_allow_always_on_sudo_covers_that_program_only() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as bash_spec

    engine = PermissionEngine("on_write")
    listing = {"command": "sudo apt list --upgradable"}
    engine.allow_always(bash_spec.name, bash_spec.scope_of(listing))
    assert engine.decide(bash_spec, listing).source == "session"
    assert engine.decide(bash_spec, {"command": "sudo apt update"}).source == "session"
    assert engine.decide(bash_spec, {"command": "sudo rm -rf /"}).decision == "ask"
    assert engine.decide(bash_spec, {"command": "sudo -u root apt list"}).decision == "ask"


def test_allow_always_on_npm_run_does_not_cover_npm_install() -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as bash_spec

    engine = PermissionEngine("on_write")
    build = {"command": "npm run build"}
    engine.allow_always(bash_spec.name, bash_spec.scope_of(build))
    assert engine.decide(bash_spec, {"command": "npm run test"}).source == "session"
    assert engine.decide(bash_spec, {"command": "npm install evil"}).decision == "ask"
    assert engine.decide(bash_spec, {"command": "npm run build && rm -rf /"}).decision == "ask"
