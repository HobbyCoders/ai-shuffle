"""Deterministic just-in-time triggers for memory facts (Memory v3, phase 0).

A fact's when-line says, in prose, the situation in which a future agent
should read it. This module turns that line into machine triggers and
matches them against a user turn (or a delegation brief) and a tool call:

- ``words``: content stems and two-word phrases, matched against a user
  turn or a delegation brief (Task, StartSession, SendMessage ...).
  Precision first: the evidence must sit within WINDOW content words, weigh
  at least WORD_THRESHOLD (a phrase 2, a distinct strong word 1, a weak word
  such as ``pr`` or ``ci`` nothing), and cover more than half of one
  alternative the when-line names. Clauses that say "do not ..." are skipped.
- ``tools``: tool names (MCP prefixes stripped: ``mcp__x__Task`` is
  ``Task``). A tool trigger needs a word match in the tool's input too,
  unless the fact has no words.
- ``command``: whole-word substrings of a tool's command text (``gh pr
  create``), or anchored patterns written ``re:<regex>`` (``git push -u``
  for a first push). Heredoc bodies and long quoted strings are not
  searched: a commit message that mentions ``git push`` is not a push.
- ``paths``: globs matched against file-tool paths; when the when-line is
  about changing files, only editing tools match.

A when-line that names a place (another checkout, a platform, a machine)
keeps only word triggers and requires that name. Facts tagged ALWAYS,
identity facts, behaviour rules and Recall-only pair answers are never
candidates: the snapshot already shows the first three by text.

Pure: no model call, no I/O, no app import, so standalone Fluid keeps it.
Nothing here is wired into prompts, Remember, storage or replication yet;
``tools/research/memory_trigger_replay.py`` measures it offline. See
``fluid/docs/research/memory-v3-proposal.md``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fluid.codemode.behavior import is_rule_key
from fluid.codemode.facts import always_applies, derived_when, is_superseded
from fluid.codemode.relevance import _STOP, _recall_text, _recall_words

# Identity facts render by their text in every snapshot (mirrors the set in
# SkillStore.render_memory_section); pair answers are Recall-only.
IDENTITY_KEYS = frozenset({"profile.name", "profile.preferred_name", "profile.pronouns",
                           "profile.language", "profile.github_username"})
_RECALL_ONLY_PREFIX = "memory.coexist."

# Words that name the setting rather than the situation: they appear in most
# when-lines (or most turns) and never tell two facts apart.
GENERIC = frozenset("""
ai shuffle fluid owner owners agent agents session sessions chat chats work working
deciding decide decides whether using used doing trying handling something anything
about after before while during also such other another only all both either rather
than not instead any every each own his her its asks ask asked new fresh way ways
case cases time times one two first last still same without against between via
within across onto per etc thing stuff someone anyone app application applying
apply rule rules learned behaviour behavior these this those under over into
""".split())
# Real subjects and verbs too common in coding turns to tell facts apart:
# they count toward a phrase, never as one of the two words a fact needs.
WEAK = frozenset("""
pr prs ci ui api cli test tests testing fix fixes fixing build builds code change changes
changing check checks checking branch branches commit commits file files run running
github git worker workers project projects repo repository main merge merging update
updating local review reviewing error errors issue issues setting settings start
starting open opening add adding write writing create creating make making get
debug debugging verify verifying job jobs fail fails failing failed pull request
requests edit editing load loading take taking tell telling move moving select
selecting prepare preparing point include including connect connecting assess
evaluate investigate implement implementing design designing plan planning choose
choosing switch launch hand off under over through look looking find finding actual
remain contain support feature features value values row rows area map list key
model models version text body path link url docs script node shell setup default
history filter account record pass block gate call calling wait waiting target
continue continuing reason report action author learn locate place may once finish
finished collect collecting evidence instance browser frontend backend server
message messages name named data user output input type new old show
""".split())

# Tool names a when-line may cite by name (capitalised, as in the catalog).
KNOWN_TOOLS = frozenset("""
Bash Read Edit Write MultiEdit NotebookEdit Glob Grep Task Agent WebFetch WebSearch
Recall Remember Forget Monitor TaskOutput TaskStop ScheduleWakeup SendMessage
StartSession HandOffSession MessageSession WorkingRecord Skill Plan
""".split())
FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
DELEGATION_TOOLS = frozenset({"Task", "Agent", "StartSession", "SendMessage", "MessageSession"})

# A clause that says what not to do ("Do not edit, commit or push") is not
# the situation it names; briefs open with exactly such lists.
_NEGATION = re.compile(r"\b(?:not|never|no|nor|don't|dont|doesn't|without|avoid)\b", re.I)

# Where a program runs: the start of a command (after a separator and any
# VAR=value prefixes), after a directory, or after a launcher word. Not as
# an argument ("grep pytest", "cat curl.log").
_RUNS = (r"(?:(?:^|[;&|(]\s*)(?:\w+=\S*\s+)*|/"
         r"|(?<!\S)(?:-m|run|exec|sudo|time|xargs)\s+)")

# Situation words in a when-line that imply a command the agent will run.
# (when-line pattern, commands). Checked on the casefolded line. A command
# is a whole-word substring of the call's command text, or, written
# "re:<pattern>", a regular expression searched in it.
_COMMAND_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(pattern), commands) for pattern, commands in (
        (r"\b(?:open|opening|opens|create|creating|file|filing)\b[^,;]*\b(?:prs?|pull requests?)\b",
         ("gh pr create",)),
        (r"\bmerg(?:e|es|ed|ing)\b[^,;]*\b(?:prs?|pull requests?)\b"
         r"|\b(?:prs?|pull requests?)\b[^,;]*\bmerg(?:e|es|ed|ing)\b", ("gh pr merge",)),
        # A first push sets the upstream; later pushes are another situation.
        (r"\bfirst push\b", (r"re:\bgit push\b[^|;&]*(?:\s-u\b|--set-upstream)",)),
        (r"(?<!first )\bpush(?:es|ed|ing)?\b", (r"re:\bgit push\b(?![^|;&]*\s(?:--delete|-d)\b)",)),
        (r"\bcommit (?:messages?|trailers?)\b|\bpr commits?\b|\bcommitting\b|\bdco\b|\btrailers?\b",
         ("git commit",)),
        (r"\b(?:creat\w*|cut\w*|nam\w*)\b[^,;]*\bbranch(?:es)?\b",
         ("git checkout -b", "git switch -c")),
        # A bare program name counts only where it runs (after a separator or
        # a path), not inside a grep pattern or a file name. A mutating call
        # is the situation, not a read: it needs a method or body flag.
        (r"\bmutat\w*\b[^,;]*\bcurl\b|\bcurl\b[^,;]*\bmutat\w*",
         ("re:" + _RUNS + r"curl\s[^|;&]*(?:-x\s*(?:post|put|patch|delete)\b|--request\s"
          r"|--data|--json\s|\s-d\s*['\"{@$])",)),
        (r"^(?!.*\bmutat).*\bcurl\b", ("re:" + _RUNS + r"curl\s",)),
        (r"\bpytest\b|\bpython tests?\b", ("re:" + _RUNS + r"pytest(?:\s|$)",)),
        (r"\bdelet\w*\b[^,;]*\bworktrees?\b|\bworktrees?\b[^,;]*\bdelet\w*", ("git worktree remove",)),
        (r"\b(?:job|actions?|ci) logs?\b|\blogs? (?:of|from) (?:a |the )?(?:ci|github actions)\b",
         (r"re:\bgh run view\b[^|;&]*--log", r"re:\bgh api\b[^|;&]*/logs\b")),
        (r"\bissue bod(?:y|ies)\b|\bview\w* (?:an |the )?\w* ?issue\b", ("gh issue view",)),
        (r"\bdocker image\b", ("docker build",)),
        (r"\bpostgres(?:ql)?\b", ("re:" + _RUNS + r"psql\s+[-\"'$]",)),
    ))
# A when-line that names where it applies (another checkout or fork, a
# platform or machine) keeps only word triggers, and they need that name in
# the turn: a command, path or common word cannot tell which checkout or
# machine the agent is on. The name is a required word, written "+name".
_PLACE = re.compile(
    r"\b(?:in|on|from|for)\s+(?:an?\s+|the\s+|this\s+)?(?!AI\b)(?P<name>[A-Z][\w-]*)(?:'s)?\s+"
    r"(?:checkout|fork|repo|repository|worktree|worker|machine|host|pc)s?\b"
    r"|\b(?:on|from)\s+(?:an?\s+)?(?P<os>Windows|Linux|macOS|Mac)\b"
    r"|\bthis (?:PC|machine|computer)\b(?:\s*\((?P<pc>[A-Z][\w-]*)\))?")
REQUIRED = "+"
_NAME = re.compile(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)+\b")

_PATH = re.compile(r"(?<![\w/.@:-])((?:[a-z0-9_.*-]+/)+[a-z0-9_.*-]+/?|[a-z0-9_-]+\.(?:py|ts|js|mjs|"
                   r"svelte|md|json|toml|ya?ml|sh|ps1|kt|cfg|ini))(?![\w/-])")
_BACKTICK = re.compile(r"`([^`\n]{3,80})`")
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b")
_SEGMENT = re.compile(r"[,;:!?()\[\]\n]|\.(?:\s|$)|\s[-–—]\s|\bor\b|\band\b", re.I)
_YEAR = re.compile(r"(?:19|20)\d\d")
_ALTERNATIVE = re.compile(r"[,;]|\bor\b", re.I)
_EDITING = re.compile(r"\b(?:chang|edit|writ|updat|add|creat|modif|renam|mov|delet)\w*")
# A slash phrase is a path when it has a file name, a glob, three parts or
# a usual top-level directory; "installing/updating" is prose.
_PATH_ROOTS = frozenset("app src docs frontend fluid tests test scripts tools mobile android desktop "
                        "lib packages .github .fluid .claude config".split())
_LEAD = re.compile(r"^\s*(?:when|whenever|before|after|while|if|once)\b\s*", re.I)
_GENERIC_WHEN = re.compile(r"^when (?:applying these owner rules|working with the owner\b)", re.I)
_SUFFIXES = ("ings", "ing", "ies", "ied", "ed", "es", "s", "e")


@dataclass(frozen=True)
class Triggers:
    """Machine triggers for one fact; every field may be empty."""
    words: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    # Strong word stems of each alternative the when-line names ("A, B or
    # C"); a word match must cover more than half of one of them.
    clauses: tuple[tuple[str, ...], ...] = ()
    # Multi-word proper names, as space-joined stems ("claud code cli").
    names: tuple[str, ...] = ()
    # Paths fire only on tools that change files: the situation is changing
    # them, not reading them.
    edit_paths: bool = False

    def __bool__(self) -> bool:
        return bool(self.words or self.tools or self.command or self.paths)


@dataclass(frozen=True)
class TriggerFact:
    """A delivery candidate: a fact id, its when-line and its triggers."""
    id: str
    when: str
    triggers: Triggers
    chars: int = 0


@dataclass(frozen=True)
class Match:
    """One fact firing on one event. ``kind`` is words, tool, command or path."""
    fact_id: str
    kind: str
    score: int
    terms: tuple[str, ...] = field(default=())


def stem(word: str) -> str:
    """A light suffix strip so merge/merged/merging and journey/journeys meet.

    Never shortens below four characters, so short subjects (ci, pr, set)
    stay whole words."""
    word = word.casefold()
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            if suffix in ("ies", "ied"):
                word += "y"
            break
    if len(word) >= 5 and word[-1] == word[-2] and word[-1] not in "lsz":
        word = word[:-1]
    return word


_WEAK_STEMS = frozenset(map(stem, WEAK))


def _strong(term: str) -> bool:
    return (term.isdigit() and len(term) >= 3) or (term not in _WEAK_STEMS and not term.isdigit())


def normalize_tool(name: str) -> str:
    """``mcp__server__Bash`` and ``Bash`` are the same tool."""
    return str(name or "").rsplit("__", 1)[-1]


def _segments(text: str) -> list[list[str]]:
    """Runs of adjacent content stems. Stop words are skipped ("open a PR"
    is open, pr); punctuation, "and"/"or", generic words, years and other
    short tokens end a run, so a phrase is two subject words side by side."""
    out = []
    for part in _SEGMENT.split(_recall_text(text)):
        run: list[str] = []
        for word in _recall_words(part):
            stemmed = stem(word)
            if word in _STOP or stemmed in _STOP:
                continue
            if (word in GENERIC or stemmed in GENERIC or _YEAR.fullmatch(word)
                    or (len(word) < 3 and word not in WEAK)
                    or (word.isdigit() and len(word) < 3)):
                if run:
                    out.append(run)
                run = []
                continue
            run.append(stemmed)
        if run:
            out.append(run)
    return out


def _glob(path: str) -> str:
    path = path.strip("/")
    if "." in path.rsplit("/", 1)[-1] or "*" in path:
        return "**/" + path
    return "**/" + path + "/**"


def derive_triggers(when: str | None, text: str | None = None, key: str | None = None) -> Triggers:
    """Triggers from a when-line, without a model.

    The fact's text is read only when the when-line is missing or generic
    (a derived first sentence, or a learner placeholder such as "When working
    with the owner"); a written situation is the owner's or saving agent's
    statement of when the fact matters, and text words would widen it.
    """
    line = " ".join(str(when or "").split())
    if not line or _GENERIC_WHEN.match(line) or (text and line == derived_when(text)):
        line = derived_when(text or "") or line
    body = _LEAD.sub("", line)
    folded = body.casefold()

    command: list[str] = []
    paths: list[str] = []
    tools: list[str] = []
    for quoted in _BACKTICK.findall(body):
        (paths if "/" in quoted and " " not in quoted else command).append(quoted.strip())
    for ident in _IDENTIFIER.findall(folded):
        command.append(ident)
    # A question ("the owner asks why ...") or a negated situation ("has not
    # merged") names no action the agent takes, so implies no command.
    if not (_NEGATION.search(folded) or re.search(r"\basks?\b", folded)):
        for pattern, commands in _COMMAND_RULES:
            if pattern.search(folded):
                command.extend(commands)
    for raw in _PATH.findall(body):
        parts = [part for part in raw.split("/") if part]
        if not any(len(part) >= 3 for part in parts):
            continue
        if not ("." in parts[-1] or "*" in raw or len(parts) >= 3 or parts[0] in _PATH_ROOTS):
            continue
        paths.append(_glob(raw))
    for token in re.findall(r"\b[A-Z][A-Za-z]+\b", body):
        if token in KNOWN_TOOLS:
            tools.append(token)
    if key and key.startswith("env."):
        program = key.removeprefix("env.").replace("_", " ")
        command.append("re:" + _RUNS + re.escape(program) + r"(?:\s|$)")
    words: list[str] = []
    place = _PLACE.search(body)
    if place:
        command, paths, tools = [], [], []
        name = place.group("name") or place.group("os") or place.group("pc")
        if name:
            words.append(REQUIRED + stem(name))
    for seg in _segments(_PATH.sub(" ", body)):
        for a, b in zip(seg, seg[1:]):
            if a != b and (_strong(a) or _strong(b)):
                words.append(f"{a} {b}")
        words.extend(seg)
    # A multi-word proper name ("Claude Code CLI", "GitHub Actions") is one
    # subject however many words it has: it counts once toward a clause and
    # its inner pairs weigh as one word, so naming a product is not a situation.
    names = []
    for found in _NAME.findall(_PATH.sub(" ", body)):
        stems = [s for seg in _segments(found) for s in seg]
        if len(stems) >= 2:
            names.append(" ".join(stems))
    unit_of = {part: name for name in names for part in name.split()}
    clauses = []
    for alternative in _ALTERNATIVE.split(_PATH.sub(" ", body)):
        # Weak words count here: "defaults to ... settings" is half the
        # situation even though neither word tells facts apart.
        units = tuple(dict.fromkeys(unit_of.get(s, s) for seg in _segments(alternative)
                                    for s in seg if not s.isdigit()))
        if units:
            clauses.append(units)
    return Triggers(
        words=tuple(dict.fromkeys(words)),
        tools=tuple(dict.fromkeys(tools)),
        command=tuple(dict.fromkeys(c if c.startswith("re:") else c.casefold() for c in command)),
        paths=tuple(dict.fromkeys(paths)),
        clauses=tuple(clauses),
        names=tuple(dict.fromkeys(names)),
        edit_paths=bool(paths and _EDITING.search(folded)),
    )


def is_candidate(entry: Mapping[str, Any]) -> bool:
    """A fact delivery can add something to: live, current, and not already
    in every snapshot (ALWAYS, identity, behaviour rules) or Recall-only."""
    key = str(entry.get("key") or "")
    return not (always_applies(entry) or is_superseded(entry) or key in IDENTITY_KEYS
                or is_rule_key(key) or key.startswith(_RECALL_ONLY_PREFIX))


def prepare(entries: Iterable[Mapping[str, Any]]) -> list[TriggerFact]:
    """Delivery candidates with derived triggers, from store entry dicts."""
    out = []
    for entry in entries:
        if not is_candidate(entry):
            continue
        when = str(entry.get("when") or "")
        text = str(entry.get("text") or "")
        triggers = derive_triggers(when, text, entry.get("key"))
        if triggers:
            out.append(TriggerFact(str(entry.get("id")), when or derived_when(text), triggers, len(text)))
    return out


_CLAUSE = re.compile(r"[.;:!?\n]+(?:\s|$)")
# Word triggers fire only when their evidence sits close together: WINDOW
# content words, about one sentence. A long brief mentions everything once.
WINDOW = 12
# Evidence needed in one window: a phrase counts PHRASE_WEIGHT, a distinct
# strong word 1, a weak word nothing; the total must reach WORD_THRESHOLD,
# and the words found (weak ones included, a proper name once) must be
# more than half of one alternative of the when-line (Triggers.clauses), so
# two topic words common to a whole project do not stand in for the situation.
PHRASE_WEIGHT = 2
WORD_THRESHOLD = 3


class _Text:
    """A turn or brief, indexed once for every fact: content stems in order
    (negated clauses dropped), and the stems and adjacent pairs it holds."""

    def __init__(self, text: str, limit: int = 6000):
        self.order: list[tuple[int, str]] = []
        pairs: list[tuple[int, str]] = []
        pos = 0
        for clause in _CLAUSE.split(text[:limit]):
            if _NEGATION.search(clause):
                continue
            for seg in _segments(clause):
                for i, s in enumerate(seg):
                    self.order.append((pos + i, s))
                    if i:
                        pairs.append((pos + i, f"{seg[i - 1]} {s}"))
                pos += len(seg) + 1
        self.pair_order = pairs
        self.stems = frozenset(s for _, s in self.order)
        self.pairs = frozenset(p for _, p in pairs)


def _singles(triggers: Triggers) -> list[str]:
    return [w for w in triggers.words if " " not in w and not w.startswith(REQUIRED)]


def _name_pairs(triggers: Triggers) -> frozenset[str]:
    return frozenset(f"{a} {b}" for name in triggers.names
                     for a, b in zip(name.split(), name.split()[1:]))


def _word_match(triggers: Triggers, text: _Text) -> tuple[int, tuple[str, ...]]:
    """(score, matched terms) of the best WINDOW; 0 below WORD_THRESHOLD.
    A required word ("+name") must appear in that window."""
    required = {w[1:] for w in triggers.words if w.startswith(REQUIRED)}
    if not required <= text.stems:
        return 0, ()
    phrases = {w for w in triggers.words if " " in w and w in text.pairs}
    singles = {w for w in _singles(triggers) if w in text.stems and _strong(w)}
    if not phrases and len(singles) < 2:
        return 0, ()
    name_pairs = _name_pairs(triggers)
    weak = {w for w in _singles(triggers) if w in text.stems and not _strong(w)}
    events = sorted([(p, w) for p, w in text.pair_order if w in phrases]
                    + [(p, w) for p, w in text.order if w in singles or w in required or w in weak])
    best, best_terms, start = 0, (), 0
    for end in range(len(events)):
        while events[end][0] - events[start][0] >= WINDOW:
            start += 1
        terms = dict.fromkeys(w for _, w in events[start:end + 1])
        weak_seen = {t for t in terms if t in weak}
        if not required <= terms.keys():
            continue
        covered = {part for t in terms if " " in t for part in t.split()}
        score = sum((1 if t in name_pairs else PHRASE_WEIGHT) if " " in t
                    else (0 if t in covered or t not in singles else 1) for t in terms)
        if score > best and _covers_clause(triggers, covered | set(terms) | weak_seen):
            best, best_terms = score, tuple(t for t in terms if t in singles or " " in t)
    return (best, best_terms) if best >= WORD_THRESHOLD else (0, ())


def _covers_clause(triggers: Triggers, found: set[str]) -> bool:
    """More than half of one alternative's subjects found; a proper name
    (a space-joined unit) counts once if any of its words is found."""
    if not triggers.clauses:
        return True
    for clause in triggers.clauses:
        hits = sum(1 for unit in clause
                   if (any(part in found for part in unit.split()) if " " in unit else unit in found))
        if 2 * hits > len(clause):
            return True
    return False


def match_turn(text: str, facts: Iterable[TriggerFact]) -> list[Match]:
    """Facts whose words fire on a user turn or brief, best first."""
    indexed = _Text(text)
    matches = []
    for fact in facts:
        score, terms = _word_match(fact.triggers, indexed)
        if score:
            matches.append(Match(fact.id, "words", score, terms))
    return sorted(matches, key=lambda m: -m.score)


# Text a command carries rather than runs: a heredoc body, a long quoted
# string (a commit message, an inline script, a brief written to a file).
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?(?:\n\1\b|\Z)", re.S)
_LONG_QUOTED = re.compile(r"'[^']{120,}'|\"(?:[^\"\\]|\\.){120,}\"", re.S)


def command_text(tool_input: Mapping[str, Any]) -> str:
    """The casefolded command a call runs, without the text it carries."""
    parts = [str(tool_input.get(k)) for k in ("command", "cmd", "script", "code")
             if isinstance(tool_input.get(k), str)]
    text = _LONG_QUOTED.sub(" ", _HEREDOC.sub(" ", "\n".join(parts)))
    # A line break separates commands; a backslash before it continues one.
    text = text.replace("\\\n", " ").replace("\n", " ; ")
    return " ".join(text.casefold().split())


def _paths(tool_input: Mapping[str, Any]) -> list[str]:
    out = []
    for k in ("file_path", "path", "notebook_path", "pattern"):
        value = tool_input.get(k)
        if isinstance(value, str) and value:
            out.append(value.replace("\\", "/"))
    return out


@lru_cache(maxsize=1024)
def _edge_pattern(needle: str) -> re.Pattern[str]:
    if needle.startswith("re:"):
        return re.compile(needle[3:])
    left = r"(?<![\w-])" if needle[:1].isalnum() or needle[:1] == "_" else ""
    right = r"(?![\w-])" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
    return re.compile(left + re.escape(needle) + right)


@lru_cache(maxsize=1024)
def _glob_pattern(glob: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out, i = out + "(?:/.*)?", i + 3
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out, re.I)


def _input_text(tool_input: Mapping[str, Any], limit: int = 6000) -> str:
    parts = [v for v in tool_input.values() if isinstance(v, str)]
    return " ".join(parts)[:limit]


def match_tool(name: str, tool_input: Mapping[str, Any] | None,
               facts: Iterable[TriggerFact]) -> list[Match]:
    """Facts whose command, path, tool or (for delegation) words fire on a
    tool call, best first. ``tool_input`` is the call's argument mapping."""
    tool = normalize_tool(name)
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    command = command_text(tool_input)
    paths = _paths(tool_input) if tool in FILE_TOOLS else []
    brief = _Text(_input_text(tool_input)) if tool in DELEGATION_TOOLS else None
    input_text: _Text | None = None
    matches = []
    for fact in facts:
        t = fact.triggers
        hit = next((c for c in t.command if command and _edge_pattern(c).search(command)), None)
        if hit:
            matches.append(Match(fact.id, "command", 3, (hit,)))
            continue
        glob = next((g for g in t.paths for p in paths
                     if (tool in EDIT_TOOLS or not t.edit_paths) and _glob_pattern(g).fullmatch(p)), None)
        if glob:
            matches.append(Match(fact.id, "path", 3, (glob,)))
            continue
        if tool in t.tools:
            # The tool alone is not the situation ("starting a code review
            # Task"): its input must carry the when-line's words too.
            if input_text is None:
                input_text = brief or _Text(_input_text(tool_input))
            if not t.words or _word_match(t, input_text)[0]:
                matches.append(Match(fact.id, "tool", 2, (tool,)))
                continue
        if brief is not None:
            score, terms = _word_match(t, brief)
            if score:
                matches.append(Match(fact.id, "words", score, terms))
    return sorted(matches, key=lambda m: -m.score)
