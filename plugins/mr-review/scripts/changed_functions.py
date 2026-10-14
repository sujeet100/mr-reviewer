#!/usr/bin/env python3
"""Map a change's edited lines onto the functions that contain them.

The ``/review-changes`` skill reviews your local work one function at a time, so it needs to
know which functions you actually touched. Asking the model to work that out from the diff is the
obvious alternative and it is unreliable in the way that matters: a hunk header gives a
*nearby* symbol, not the enclosing one, so review units silently drift to the wrong function
or collapse into "this file". This script decides it deterministically instead, and the model
spends its attention on the code rather than on bookkeeping.

Output is a JSON array of review units, each one either a function that changed or a run of
changed lines that sits outside any function (module level, field declarations, imports).
Nothing a diff touched is dropped, because a unit list that quietly omits lines is worse than
one that admits it does not know where they belong.

    changed_functions.py                        # this branch's work, including uncommitted
    changed_functions.py --staged               # only what is staged
    changed_functions.py --uncommitted          # only what is not committed yet
    changed_functions.py --base main            # everything since main, incl. working tree
    changed_functions.py --base X --head Y      # a committed range
    changed_functions.py --self-test

The default scope is deliberately the widest useful one: the merge base with the trunk branch
compared against the **working tree**, so work in progress is reviewed rather than only what
has been committed. Reviewing before the commit is the point — that is when the feedback is
still cheap to act on.

Python spans come from ``ast`` and are exact. Brace-language spans come from a signature
regex plus a depth scan that strips strings and comments first, which is approximate at the
edges (a macro-generated body, a signature split across lines in an unusual way) and correct
on ordinary code. When a file cannot be parsed at all, its lines become out-of-function units
rather than disappearing.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- language mapping -----------------------------------------------------------------

_PY_SUFFIXES = (".py", ".pyi")
_BRACE_SUFFIXES = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".cs": "csharp",
    ".rs": "rust",
    ".swift": "swift",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}

# Test sources. Their quality is squarely in the charter's remit (assertions on mocks, missing
# negative cases, a test that would pass a wrong implementation), but they must not be allowed to
# crowd out production code: a new test is verbose and additive, so ranking units by changed-line
# count alone puts tests at the top and pushes the subtle three-line production change off the
# end of the budget. Classified here so ordering can put production first.
_TEST_PATH = re.compile(
    r"(^|/)(src/test/|src/intTest/|src/testFixtures/|tests?/|__tests__/|spec/|features/)"
    r"|(Test|Tests|IT|Spec)\.[a-z]+$"
    r"|(_test|_spec|\.test|\.spec)\.[a-z]+$"
    r"|(^|/)test_[^/]*\.py$",
    re.IGNORECASE,
)

# Not worth a function-level read: generated, locked, binary, or not code at all.
_SKIP_EXACT = {
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "gradle.lockfile",
}
_SKIP_SUFFIXES = (
    ".lock",
    ".min.js",
    ".map",
    ".snap",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".ico",
    ".pdf",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".txt",
    ".csv",
)

# --- change context -------------------------------------------------------------------
# Facts about the change that every pass can be handed without biasing any of them: what a unit
# touches (tags), who calls it (callers), whether anything tests it, and which other units in the
# same change it is coupled to. Judgement — is this risky, is it worth an agent — stays with the
# triage pass; these are the inputs to that judgement, computed once here instead of re-derived by
# every agent with its own greps. Everything in this section is deliberately over-inclusive: a tag
# costs a two-kilobyte lens file appended to one prompt, and a missed tag costs a blind spot.

_TAG_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "persistence",
        re.compile(
            r"@Transactional|Repository\b|\.save(?:All)?\(|update(?:One|Many|First)\(|bulkOps|\.commit\("
            r"|session\.(?:commit|add|flush|query|delete)|\.persist\(|\.flush\(|mongoTemplate|jdbcTemplate"
            r"|\bINSERT\s|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b|\.deleteBy|\.findBy\w*\("
        ),
    ),
    (
        "concurrency",
        re.compile(
            r"\bsynchronized\b|\bLock\b|ReentrantLock|Atomic\w+|Executor(?:Service)?\b|CompletableFuture|@Async"
            r"|@Scheduled|\bThread\b|asyncio\.(?:gather|create_task|Lock|Semaphore)|threading\.|multiprocessing"
            r"|ConcurrentHashMap|\bvolatile\b|Semaphore|Promise\.all|parallelStream"
        ),
    ),
    (
        "authz",
        re.compile(
            r"@PreAuthorize|@Secured|@RolesAllowed|hasRole|hasAuthority|[Pp]ermission|[Tt]enant\w*|ownerId"
            r"|isAdmin|[Aa]uthori[sz]|[Aa]uthenticat|SecurityContext|@AuthenticationPrincipal|current_user"
        ),
    ),
    (
        "money",
        re.compile(
            r"BigDecimal|RoundingMode|\bDecimal\(|\b[Pp]rice\w*|\b[Dd]iscount\w*|\b[Tt]ax(?:es|Rate|Amount)?\b"
            r"|[Cc]urrency|\bMoney\b|\bround\(|Math\.round|\b[Ff]ee(?:s)?\b|[Ii]nvoice|[Bb]illing|[Rr]efund"
        ),
    ),
    (
        "retry_cache",
        re.compile(
            r"@Retryable|@Cacheable|@CacheEvict|@CachePut|\b[Rr]etry\w*|[Bb]ackoff|\bttl\b|\bevict\w*"
            r"|\b[Cc]ache\w*|lru_cache|Retry\("
        ),
    ),
    (
        "boundary",
        re.compile(
            r"RestController|@Controller|@(?:Get|Post|Put|Delete|Patch|Request)Mapping|\bClient\b|Feign"
            r"|WebClient|RestTemplate|requests\.(?:get|post|put|delete)|httpx\.|\bfetch\(|HttpClient"
            r"|@RabbitListener|@KafkaListener|@app\.(?:get|post|put|delete)|router\.(?:get|post|put|delete)"
        ),
    ),
)
# Tags that make a unit high-risk on their own: being wrong there costs money, data or access.
_HIGH_RISK_TAGS = {"money", "authz", "persistence", "concurrency", "public_api"}
# The lens file a tag asks the reviewer to read in addition to its charter. Several tags share one.
_LENS_FILE = {
    "persistence": "lens_persistence.md",
    "concurrency": "lens_concurrency.md",
    "authz": "lens_security.md",
    "boundary": "lens_security.md",
    "money": "lens_money.md",
    "retry_cache": "lens_retry_cache.md",
}
# Units per agent by the riskiest unit in the batch. A high-risk function gets an agent nearly to
# itself (one tightly coupled same-file helper may ride along); everything else uses the normal cap.
_RISK_CAP = {"high": 2, "normal": 10_000}

# Added-line signals that a real design decision was made somewhere in the change. Shared with the
# CLI's MAKER gate (`orchestrator.py`), and used here for two things: suggesting MAKER, and telling
# the skill that the ARCHITECT pass has something structural to look at.
_DECISION_SIGNALS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "abstraction introduced",
        re.compile(
            r"\b(abstract\s+class|interface\s+\w+|extends\s+\w+|implements\s+\w+|ABC\b|Protocol\b)"
        ),
    ),
    (
        "untyped bag at a boundary",
        re.compile(
            r"(Map<String,\s*Object>|List<Document>|\bObject\s+\w+\s*[;,)=]|\bDocument\s+\w+\s*[;,)=]|:\s*any\b|Dict\[str,\s*Any\])"
        ),
    ),
    (
        "runtime type interrogation",
        re.compile(r"\b(instanceof|isinstance\(|\.class::cast|typeof\s)"),
    ),
    (
        "bulk data transform",
        re.compile(
            r"\b(mongoTemplate|updateFirst|updateOne|updateMany|bulkOps|findAll\(\)|createQuery|saveAll)\b"
        ),
    ),
    (
        "quality gate suppressed",
        re.compile(
            r"(CPD-OFF|@SuppressWarnings|eslint-disable|NOSONAR|# noqa|@Disabled|pragma:\s*no cover)"
        ),
    ),
    (
        "caching or scheduling choice",
        re.compile(r"@(Cacheable|CachePut|Scheduled|Async|Retryable|Transactional)\b"),
    ),
    (
        "new integration or service seam",
        re.compile(r"@(Service|Component|Repository|RestController|FeignClient)\b"),
    ),
    (
        "manufactured null or sentinel",
        re.compile(
            r"(singletonList\(null\)|asList\(null\)|\.orElse\(null\)|Optional\.empty\(\)\.get|return None\s*#|\bNone\s*,\s*#)"
        ),
    ),
)
_DECISION_PATH = re.compile(r"(migration|changelog|backfill|migrate)", re.IGNORECASE)

# --- mechanical edits -----------------------------------------------------------------
# A unit is "trivial" when every line it changed is plumbing (imports, a lone annotation, a field
# declaration with a literal initialiser, a comment) or when the edit is an identifier rename that
# the change applies in at least three places. Both have one obvious shape and a compiler or linter
# has already checked them; spending an Opus agent on them is the waste the triage pass was
# created to prevent, and this lets the script prevent the common case for free. Anything the
# filter is unsure about stays reviewable — the cost of a false "trivial" is a silently unreviewed
# function, so every rule here errs toward substance.
_STATEMENT_START = re.compile(
    r"^\s*(?:return|throw|raise|yield|await|if|elif|else|for|while|switch|case|try|catch|with|match|when|assert|del)\b"
)
_IMPORT_LINE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|package\b|using\b|require\(|export\s*\{|#include\b|export\s+\*|export\s+\{)"
)
_ANNOTATION_LINE = re.compile(r"^\s*@[\w.$]+(?:\([^)]*\))?\s*,?\s*$")
_COMMENT_LINE = re.compile(r"^\s*(?://|#|\*|/\*|\*/|\"\"\"|''')")
_MODIFIERS = r"(?:(?:public|private|protected|internal|static|final|readonly|override|open|lateinit|abstract|export|declare|val|var|let|const|optional|transient|volatile)\s+)*"
_TYPE = r"[\w$<>\[\],.?]+(?:\s*[|&]\s*[\w$<>\[\],.?]+)*"
_LITERAL = r"(?:null|None|nil|true|false|True|False|-?\d[\d_.]*[a-zA-Z]{0,2}|\"[^\"]*\"|'[^']*'|\[\]|\{\}|new\s+[\w$<>.]+\(\s*\)|[\w$.]+)"
_DECL_TYPED = re.compile(rf"^\s*{_MODIFIERS}{_TYPE}\s+[\w$]+\s*(?:=\s*{_LITERAL})?\s*[;,]?\s*$")
_DECL_ANNOTATED = re.compile(
    rf"^\s*{_MODIFIERS}[\w$]+\s*[?!]?\s*:\s*{_TYPE}\s*(?:=\s*{_LITERAL})?\s*[;,]?\s*$"
)
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_TOKEN = re.compile(
    r"[A-Za-z_$][\w$]*|\d[\w.]*|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|`(?:\\.|[^`])*`"
    r"|==|!=|<=|>=|&&|\|\||->|=>|::|\+\+|--|\S"
)
_KEYWORDS = frozenset(
    [
        "if",
        "else",
        "elif",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "return",
        "throw",
        "throws",
        "try",
        "catch",
        "finally",
        "new",
        "delete",
        "in",
        "of",
        "is",
        "not",
        "and",
        "or",
        "null",
        "None",
        "nil",
        "true",
        "false",
        "True",
        "False",
        "this",
        "self",
        "super",
        "import",
        "from",
        "as",
        "def",
        "class",
        "fun",
        "function",
        "func",
        "val",
        "var",
        "let",
        "const",
        "static",
        "public",
        "private",
        "protected",
        "final",
        "async",
        "await",
        "yield",
        "lambda",
        "with",
        "pass",
        "raise",
        "assert",
        "match",
        "when",
        "override",
        "open",
        "sealed",
        "data",
        "record",
        "enum",
        "interface",
        "extends",
        "implements",
        "void",
        "int",
        "long",
        "double",
        "float",
        "boolean",
        "bool",
        "str",
        "string",
        "number",
        "any",
        "unknown",
        "never",
        "object",
        "typeof",
        "instanceof",
        "isinstance",
    ]
)
_RENAME_MIN_SITES = 3  # an identifier substitution seen at fewer sites is a retarget, not a rename

# --- brace-language scanning ----------------------------------------------------------

# A line opens a function body when it has a parameter list and an opening brace, and is not
# one of the control constructs that share that shape. Deliberately a heuristic: the cost of a
# miss is one review unit anchored at the file instead of the function, not a lost finding.
_CONTROL_WORDS = re.compile(
    r"^\s*(?:\}\s*)?(?:if|for|while|switch|catch|else|try|do|finally|synchronized|"
    r"return|new|throw|case|default|with|match|when)\b"
)
_CONTAINER = re.compile(
    r"^\s*(?:@\w+[^\n]*\s+)*(?:public|private|protected|internal|abstract|final|sealed|open|"
    r"static|export|declare|data|\s)*\b(class|interface|enum|record|struct|trait|object|"
    r"namespace|impl)\s+([\w$<>.]+)"
)
# `foo(...) {`, `function foo(...) {`, `fun foo(...) {`, `func (r *T) foo(...) {`,
# `const foo = (...) => {`, `foo = async (...) => {`
_SIGNATURE = re.compile(
    r"(?:\b(?:function|fun|func|def|sub)\s+(?P<kw_name>[\w$]+)"
    r"|\b(?:const|let|var)\s+(?P<var_name>[\w$]+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\()"
    r"|(?P<plain_name>[\w$~]+)\s*(?:<[^>()]*>)?\s*\()"
)


# Test-framework grouping calls. Semantically these are containers, like a class — the scanner
# must descend into them. Treated as functions (which their shape suggests) a whole spec file
# collapsed into a single unit named `describe`, losing every individual test.
_SUITE = re.compile(r"^\s*(?:x|f)?(describe|context|suite)\b\s*(?:\.\w+)?\s*\(")

# ...and these are the functions inside them. Named explicitly because their shape is a call,
# not a declaration, so the ordinary signature rule cannot see them.
_TEST_CASE = re.compile(
    r"^\s*(?:x|f)?(it|test|specify|before(?:Each|All)|after(?:Each|All)|setup|teardown)\b"
    r"\s*(?:\.\w+)?\s*\("
)

# The label a test declares, e.g. it('rejects an empty id', ...) — read from the RAW line,
# because a test named `it` tells the reviewer nothing about what it is meant to prove.
_TEST_LABEL = re.compile(r"""\(\s*(['"`])(.+?)\1""")


def _strip_noise(line: str, in_block: bool) -> tuple[str, bool]:
    """Remove strings and comments from a line so brace counting is not fooled by them.

    Returns the cleaned line and whether a block comment is still open. Content inside
    strings and comments is replaced rather than deleted so column-free scanning stays simple.
    """
    out: list[str] = []
    i, n = 0, len(line)
    quote: str | None = None
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            break
        if ch == "#" and not out:  # shell-style comment at line start (kotlin scripts, etc.)
            break
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block


def _brace_spans(text: str) -> list[tuple[str, int, int]]:
    """Find (qualified_name, start_line, end_line) for every function body in a brace file.

    One pass: clean each line, note the enclosing container when one is declared, and when a
    signature line opens a body, follow brace depth until it closes.
    """
    raw_lines = text.splitlines()
    cleaned: list[str] = []
    in_block = False
    for raw in raw_lines:
        c, in_block = _strip_noise(raw, in_block)
        cleaned.append(c)

    spans: list[tuple[str, int, int]] = []
    containers: list[tuple[str, int]] = []  # (name, depth_at_declaration)
    depth = 0
    i = 0
    while i < len(cleaned):
        line = cleaned[i]

        is_suite = False
        container = _CONTAINER.match(line)
        if container:
            containers.append((container.group(2), depth))
        else:
            suite = _SUITE.match(line)
            if suite:
                containers.append((_test_label(raw_lines[i]) or suite.group(1), depth))
                is_suite = True

        # A suite line is a container and nothing else. Without this guard it also matched the
        # signature rule, which put the whole spec file back into one unit named `describe`.
        name = None if is_suite else (_test_case_name(line, raw_lines[i]) or _signature_name(line))
        if name and "{" in line:
            end = _scan_body_end(cleaned, i)
            prefix = ".".join(n for n, d in containers if d < depth + 1)
            start = _absorb_annotations(cleaned, i)
            spans.append((f"{prefix}.{name}" if prefix else name, start + 1, end + 1))
            depth += _net_braces("".join(cleaned[i : end + 1]))
            i = end + 1
            continue

        # Prune the container stack when a line closes at least as much as it opens. Pruning on
        # every line would drop a container whose brace is on the following line (`class Foo\n
        # implements Bar {`); never pruning leaves closed siblings on the stack, which is how a
        # spec file produced names like `Outer.Tags.Sorting.beforeEach` — one segment per
        # describe block that had already ended. The `net == 0` half of the test matters for a
        # container that opens and closes on one line (`class MockFoo extends Bar {}`), which
        # otherwise stays on the stack for the rest of the file.
        net = _net_braces(line)
        depth += net
        if net <= 0 and "}" in line:
            containers = [(n, d) for n, d in containers if d < max(depth, 0)]
        i += 1
    return spans


def _test_label(raw: str) -> str | None:
    """The quoted label a test or suite declares, trimmed to something readable."""
    m = _TEST_LABEL.search(raw)
    if not m:
        return None
    label = m.group(2).strip()
    return label[:60] if label else None


def _test_case_name(line: str, raw: str) -> str | None:
    """`it('places an order')` -> `it: places an order`, or None if this is not a test case."""
    m = _TEST_CASE.match(line)
    if not m:
        return None
    label = _test_label(raw)
    return f"{m.group(1)}: {label}" if label else m.group(1)


def _signature_name(line: str) -> str | None:
    """The function name a line declares, or None when it declares no function."""
    if "(" not in line or _CONTROL_WORDS.match(line) or _CONTAINER.match(line):
        return None
    m = _SIGNATURE.search(line)
    if not m:
        return None
    group = "kw_name" if m.group("kw_name") else "var_name" if m.group("var_name") else "plain_name"
    name = m.group(group)
    if not name or name in {"if", "for", "while", "switch", "catch", "return", "new"}:
        return None

    # A name reached through a receiver is a call, not a declaration: `router.get('/x', cb => {`
    # would otherwise register a function called `get`. A qualified *type* before the name always
    # has whitespace after it (`java.util.List foo(`), so only an immediately preceding dot
    # disqualifies. An assignment or arrow before the name means the same thing.
    before = line[: m.start(group)]
    if before.rstrip().endswith(".") or any(t in before for t in ("=>", "->", "=")):
        return None
    return name


_ANNOTATION = re.compile(r"^\s*@[\w$.]+\s*(?:\([^)]*\))?\s*$")


def _absorb_annotations(cleaned: list[str], signature: int) -> int:
    """Index of the first annotation line decorating this signature, else the signature itself.

    An `@Transactional` or `@Override` is part of the method's contract, so a diff that only
    touches the annotation should be reviewed as that method rather than as a stray line.
    """
    first = signature
    while first > 0 and _ANNOTATION.match(cleaned[first - 1]):
        first -= 1
    return first


def _net_braces(text: str) -> int:
    return text.count("{") - text.count("}")


def _scan_body_end(cleaned: list[str], start: int) -> int:
    """Index of the line closing the body that opens on `start`. Falls back to the file end."""
    depth = 0
    for i in range(start, len(cleaned)):
        depth += _net_braces(cleaned[i])
        if (i > start or "{" in cleaned[i]) and depth <= 0:
            return i
    return len(cleaned) - 1


def _python_spans(text: str) -> list[tuple[str, int, int]]:
    """Exact (qualified_name, start, end) spans via ast. Empty list on a syntax error."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    spans: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                spans.append((name, start, child.end_lineno or child.lineno))
                walk(child, name)
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


def _python_class_prefixes(text: str) -> set[str]:
    """The dotted prefixes in this file that name a class.

    `_python_spans` qualifies a nested function the same way it qualifies a method, so
    `pack_cards.fits` and `Card.title` are indistinguishable from the symbol alone. Grouping
    cards on the first segment therefore turned every closure into its own "class" card. Only
    `ast` knows the difference, so it is recorded here and carried on the unit.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    found: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                found.add(name)
                walk(child, name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, f"{prefix}.{child.name}" if prefix else child.name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _owner_of(symbol: str | None, classes: set[str]) -> str:
    """The class that owns this symbol, or "" when nothing does.

    For the brace languages every dotted prefix comes from `_CONTAINER`, so it already is a
    class; for Python only the prefixes `ast` called a ClassDef count.
    """
    if not symbol or "." not in symbol:
        return ""
    parts = symbol.split(".")
    if classes:
        for i in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in classes:
                return candidate
        return ""
    return parts[0]


# --- diff parsing ---------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


_TRUNK_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master", "develop")


def resolve_scope(
    repo: Path, base: str | None, head: str | None, staged: bool, uncommitted: bool
) -> tuple[list[str], str]:
    """Build the `git diff` arguments for the requested scope, plus a label for the human.

    Returned separately from the diff calls because the label is what the skill reports back:
    a review whose scope the reader cannot see is a review they cannot trust.
    """
    if staged:
        return ["--cached"], "staged changes"
    if uncommitted:
        return ["HEAD"], "uncommitted changes (staged and unstaged)"
    if base and head:
        return [f"{base}..{head}"], f"committed range {base}..{head}"
    if base:
        return [base], f"everything since {base}, including the working tree"

    trunk = _trunk(repo)
    if trunk is None:
        return ["HEAD"], "uncommitted changes (no trunk branch found to compare against)"
    merge_base = _git(repo, "merge-base", trunk, "HEAD").strip()
    return [merge_base], f"this branch's work since {trunk}, including the working tree"


@dataclass(frozen=True)
class Sibling:
    """Another repository the same change touches, searched for callers and tests.

    The label is what a finding uses to say where a caller lives, and it is the repository's
    directory name because that is what the developer calls it. Nothing is diffed here: this
    repo's own changes are reviewed by its own run, and what is wanted from a sibling is only
    who calls into the change.
    """

    label: str
    path: Path


def _trunk(repo: Path) -> str | None:
    """The trunk branch to diff against, or None when none of the usual names resolve."""
    for candidate in _TRUNK_CANDIDATES:
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", candidate)
        except RuntimeError:
            continue
        if candidate == "origin/HEAD":
            # Symbolic: dereference it to the branch it points at, e.g. origin/main.
            try:
                return _git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD").strip()
            except RuntimeError:
                continue
        return candidate
    return None


@dataclass
class Hunk:
    """One `-U0` hunk: the new-side start line, the lines removed and the lines added."""

    new_start: int
    removed: list[str]
    added: list[str]

    @property
    def new_lines(self) -> list[int]:
        """New-side line numbers this hunk touches.

        A hunk that only deletes touches no new line, but the deletion still happened *somewhere*:
        anchoring it at the line that now follows the gap lets the enclosing function become a
        unit. Without this, a removed guard clause was invisible to the whole review.
        """
        if self.added:
            return list(range(self.new_start, self.new_start + len(self.added)))
        return [max(1, self.new_start)] if self.removed else []


_HUNK_CACHE: dict[tuple[str, tuple[str, ...], str], list[Hunk]] = {}


def _hunks(repo: Path, scope: list[str], path: str) -> list[Hunk]:
    """The `-U0` hunks for one file, both sides, cached so callers and triviality share one diff."""
    key = (str(repo), tuple(scope), path)
    if key in _HUNK_CACHE:
        return _HUNK_CACHE[key]
    out = _git(repo, "diff", "-U0", "--no-color", *scope, "--", path)
    hunks: list[Hunk] = []
    current: Hunk | None = None
    for line in out.splitlines():
        m = _HUNK.match(line)
        if m:
            current = Hunk(int(m.group(1)), [], [])
            hunks.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current.added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current.removed.append(line[1:])
    _HUNK_CACHE[key] = hunks
    return hunks


def _changed_lines(repo: Path, scope: list[str], path: str) -> list[int]:
    """New-side line numbers this change added, modified, or deleted at, for one file."""
    lines: list[int] = []
    for h in _hunks(repo, scope, path):
        lines.extend(h.new_lines)
    return sorted(set(lines))


def _changed_files(repo: Path, scope: list[str]) -> list[str]:
    out = _git(repo, "diff", "--name-only", "--diff-filter=d", *scope)
    return [p for p in out.splitlines() if p.strip()]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


# --- units ----------------------------------------------------------------------------

# --- batching ---------------------------------------------------------------------------
# One agent per function is the most favourable condition a reviewer can have, but it is also the
# most expensive, and several functions from the SAME file cost almost nothing extra: the agent
# already reads the whole file for context. The register shift that batching causes came from
# holding many *files* in mind, not several functions of one file — so batches never span files,
# and stay small enough that the last function in a batch still gets real attention.
BATCH_MAX_UNITS = 4  # functions per agent
BATCH_MAX_LINES = 400  # changed lines per agent
SOLO_CHANGED_LINES = 150  # a unit this large reviews alone
# Attention is spent reading the *function*, not the diff: ten changed lines inside a 500-line
# method cost more to review well than sixty across four short ones. So batches are also bounded by
# the total body the agent has to hold, and a very long function reviews alone regardless.
BATCH_MAX_BODY_LINES = 300  # summed function-body lines per agent
SOLO_BODY_LINES = 200  # a function this long reviews alone

# Hard ceiling on agents, because each one is a real cost and a large branch has no natural
# bound. Production batches are selected first (see the ordering note on `_TEST_PATH`), but a
# slice is reserved for tests: test quality is in the charter's remit, and a pure
# production-first cut means a test-heavy branch never gets a single test reviewed.
MAX_BATCHES = 12
TEST_BATCH_SHARE = 0.25  # of the budget, reserved for tests when any exist

# --- cards and invocations --------------------------------------------------------------
# The semantic review unit and the agent invocation are NOT the same thing, and conflating them
# was the original design error. A `Card` is one coherent thing to judge — a new file, a test
# suite, a class's changed methods — sized with no regard for cost. A `Batch` is one agent call,
# sized with no regard for coherence, holding however many cards fit its budget. Keeping them
# separate is what lets a 14-line card share an invocation with five others instead of spending a
# whole agent (and ~12k tokens of re-read charter) on it, while a 350-line new test module stays
# ONE review target instead of being shredded into twelve.
#
# Measured on a large reference MR (68 files, 2671 insertions): one-agent-per-batch produced 76
# agents reading 22,297 lines to review 1,459 changed ones, because every batch of a file re-read
# that whole file. Cards plus packing covers the same 207 units in a fraction of the invocations.

# What the agent must actually read for a card. Only the *duplication* was waste: the total lines
# a change requires is a property of the change, so this bounds how it is sliced, not the sum.
CTX_LINES = 40  # lines of surrounding context per changed region, when not reading whole
WHOLE_FILE_FRAC = 0.5  # read the whole file only once this much of it changed
CARD_MAX_READ = 700  # a single card this large is split; above the invocation budget on
# purpose, so a big cohesive file stays one target rather than two

# Invocation budget. Lines dominate cost, but semantic load is the thing that degrades review
# quality, so both are bounded: an LLM having room in its context window does not mean it uses
# all of that window evenly. These are HYPOTHESES to be benchmarked, not measured cliffs — see
# the canary experiment noted in the skill's docs.
INVOCATION_MAX_READ = 600  # lines of code one agent reads across all its cards
INVOCATION_MAX_CARDS = 6  # independent review targets per agent
INVOCATION_MAX_CHANGED = 150  # changed lines per agent
INVOCATION_MAX_SYMBOLS = 12  # changed functions per agent, across all its cards
# Beyond this many independent targets the "treat each card separately" instruction is probably
# not holding. Also a guess; it is the point at which the tool warns, not a known limit.
CARD_WARN_PER_INVOCATION = 6


@dataclass
class Unit:
    file: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    changed_lines: list[int]
    changed_count: int
    file_lines: int
    is_test: bool = False
    # Everything below is change context (see the section of that name): facts computed once and
    # handed to every pass, never judgement. Defaults keep the positional constructor stable.
    removed_count: int = 0  # old-side lines deleted inside this unit
    trivial: bool = False  # every changed line is plumbing or a rename
    trivial_reason: str = ""
    tags: list[str] = field(default_factory=list)
    risk: str = "normal"  # "high" | "normal"; a floor, triage may raise it
    owner: str = ""  # the class that owns this unit, or "" — see _owner_of
    callers: list[str] = field(default_factory=list)  # file:line, outside this unit, capped
    caller_count: int = 0
    callers_in_tests: int = 0
    callers_in_siblings: int = 0  # call sites in a sibling repo, prefixed `<label>:`
    tests_in_change: list[str] = field(default_factory=list)  # ids of test units that name it
    siblings: list[str] = field(default_factory=list)  # ids of call-linked units in the change

    @property
    def id(self) -> str:
        """Stable handle for this unit, for a triage pass to select by.

        Derived rather than generated so it survives a re-run: the triage agent picks ids from
        one invocation and `--select` resolves them in the next, with no state in between.
        """
        return f"{self.file}:{self.start_line}-{self.end_line}"

    @property
    def body_lines(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def simple_name(self) -> str | None:
        """The bare function name, for grepping callers. None for test cases and orphan runs."""
        if not self.symbol:
            return None
        last = self.symbol.split(".")[-1]
        return None if ":" in last else last

    @property
    def untested(self) -> bool:
        """A substantive production change that nothing in the change or the repo exercises."""
        return (
            not self.is_test
            and not self.trivial
            and bool(self.simple_name)
            and not self.tests_in_change
            and self.callers_in_tests == 0
        )

    @property
    def lenses(self) -> list[str]:
        return sorted({_LENS_FILE[t] for t in self.tags if t in _LENS_FILE})


@dataclass
class Card:
    """One coherent review target: what a reviewer judges as a single piece of work.

    Always within one file, because a card is a claim about cohesion and nothing spanning files
    is cohesive enough to judge as one thing — that is what the architecture and integration
    passes are for. `kind` records *why* these units are one card, which the brief shows the
    agent so it knows whether it is looking at a whole new artifact or a slice of an old one.
    """

    file: str
    language: str
    is_test: bool
    kind: str  # new-file | suite | class | region
    units: list[Unit]
    read_lines: int

    @property
    def changed_count(self) -> int:
        return sum(u.changed_count for u in self.units)

    @property
    def risk(self) -> str:
        return "high" if any(u.risk == "high" for u in self.units) else "normal"

    @property
    def tags(self) -> list[str]:
        return sorted({t for u in self.units for t in u.tags})

    @property
    def lenses(self) -> list[str]:
        return sorted({lens for u in self.units for lens in u.lenses})

    @property
    def title(self) -> str:
        """How to name this card to a human, and to the agent reviewing it."""
        if self.kind == "new-file":
            return f"{self.file} (new file, reviewed whole)"
        if self.kind == "suite":
            return f"{self.file} (test suite)"
        if self.kind == "class":
            return f"{self.file} :: {self.units[0].owner}"
        return f"{self.file} ({len(self.units)} changed region(s))"


@dataclass
class Batch:
    files: list[str]
    language: str
    is_test: bool
    changed_count: int
    units: list[Unit]
    # The review targets inside this invocation. Empty on batches built by the older
    # unit-to-batch primitives (`batch_units`, `fit_to_budget`), which the triage and `--fit`
    # paths still use; the default path builds these through `make_cards`/`pack_cards`.
    cards: list[Card] = field(default_factory=list)

    @property
    def label(self) -> str:
        """How to name this batch to a human."""
        if len(self.files) == 1:
            return self.files[0]
        return f"{len(self.files)} files in {_common_dir(self.files)}"

    @property
    def read_lines(self) -> int:
        """Lines of code this invocation must read — the thing that actually costs."""
        if self.cards:
            return sum(c.read_lines for c in self.cards)
        by_file: dict[str, list[Unit]] = {}
        for u in self.units:
            by_file.setdefault(u.file, []).append(u)
        return sum(read_lines_for(g) for g in by_file.values())

    @property
    def spans_files(self) -> bool:
        return len(self.files) > 1

    @property
    def risk(self) -> str:
        return "high" if any(u.risk == "high" for u in self.units) else "normal"

    @property
    def tags(self) -> list[str]:
        return sorted({t for u in self.units for t in u.tags})

    @property
    def lenses(self) -> list[str]:
        return sorted({lens for u in self.units for lens in u.lenses})

    @property
    def body_lines(self) -> int:
        return sum(u.body_lines for u in self.units)


def _reviewable(path: str) -> str | None:
    """The language to review this path as, or None when it is not function-reviewable."""
    name = Path(path).name
    if name in _SKIP_EXACT or path.endswith(_SKIP_SUFFIXES):
        return None
    suffix = Path(path).suffix
    if suffix in _PY_SUFFIXES:
        return "python"
    return _BRACE_SUFFIXES.get(suffix)


def _group_runs(numbers: list[int]) -> list[list[int]]:
    """Split sorted line numbers into contiguous-ish runs, so unrelated edits stay separate."""
    runs: list[list[int]] = []
    for n in sorted(numbers):
        if runs and n - runs[-1][-1] <= 3:
            runs[-1].append(n)
        else:
            runs.append([n])
    return runs


def units_for_file(
    path: str, language: str, text: str, changed: list[int], hunks: list[Hunk] | None = None
) -> list[Unit]:
    """Split one file's changed lines into per-function units plus out-of-function runs.

    When the hunks are given, each unit also learns how many lines were deleted inside it and
    whether its edit was mechanical — both need the old side of the diff, which the line numbers
    alone cannot carry.
    """
    spans = _python_spans(text) if language == "python" else _brace_spans(text)
    total = len(text.splitlines())

    # Innermost span wins, so an edit inside a nested function is reviewed as that function.
    def enclosing(line: int) -> tuple[str, int, int] | None:
        hits = [s for s in spans if s[1] <= line <= s[2]]
        return min(hits, key=lambda s: s[2] - s[1]) if hits else None

    by_span: dict[tuple[str, int, int], list[int]] = {}
    orphans: list[int] = []
    for line in changed:
        span = enclosing(line)
        if span is None:
            orphans.append(line)
        else:
            by_span.setdefault(span, []).append(line)

    is_test = bool(_TEST_PATH.search(path))
    classes = _python_class_prefixes(text) if language == "python" else set()
    units = []
    for (name, start, end), lines in by_span.items():
        u = Unit(path, language, name, start, end, sorted(lines), len(lines), total, is_test)
        u.owner = _owner_of(name, classes)
        units.append(u)
    source = text.splitlines()
    for run in _group_runs(orphans):
        if not _has_substance(run, source):
            continue
        units.append(Unit(path, language, None, run[0], run[-1], run, len(run), total, is_test))

    if hunks is not None:
        for u in units:
            u.removed_count = sum(len(h.removed) for h in _hunks_touching(hunks, u))

    units.sort(key=lambda u: (-u.changed_count, u.start_line))
    return units


def _hunks_touching(hunks: list[Hunk], unit: Unit) -> list[Hunk]:
    mine = set(unit.changed_lines)
    return [h for h in hunks if mine.intersection(h.new_lines)]


# Punctuation-only lines close a block; on their own they carry nothing to review.
_TRIVIAL = re.compile(r"^[\s{}()\[\];,]*$")


def _has_substance(run: list[int], source: list[str]) -> bool:
    """True when a run of out-of-function lines contains anything worth reading.

    Without this, every blank line and stray brace between two functions becomes its own
    review unit — which on a newly added file means a dozen units that say nothing.
    """
    return any(1 <= line <= len(source) and not _TRIVIAL.match(source[line - 1]) for line in run)


def batch_units(
    units: list[Unit],
    max_units: int = BATCH_MAX_UNITS,
    max_lines: int = BATCH_MAX_LINES,
    preserve_order: bool = False,
) -> list[Batch]:
    """Group units into agent-sized batches, never spanning a file.

    One file per batch is the whole point: the agent reads that file once and reviews every
    changed function in it, so the marginal cost of the second and third function is close to
    zero. Crossing files is what would reintroduce the batching failure this pass exists to
    avoid, so it never happens here regardless of how small the batches get.
    """
    by_file: dict[str, list[Unit]] = {}
    for u in units:
        by_file.setdefault(u.file, []).append(u)

    batches: list[Batch] = []
    for path, group in by_file.items():
        current: list[Unit] = []
        lines = body = 0
        for u in _coupled_order(group):
            solo = u.changed_count >= SOLO_CHANGED_LINES or u.body_lines >= SOLO_BODY_LINES
            # A high-risk function narrows the batch it joins: it gets an agent nearly to itself,
            # and only a same-file helper it is coupled to rides along.
            cap = min(max_units, *(_RISK_CAP[x.risk] for x in current + [u]))
            too_many = len(current) >= cap
            too_big = current and (
                lines + u.changed_count > max_lines or body + u.body_lines > BATCH_MAX_BODY_LINES
            )
            if current and (solo or too_many or too_big):
                batches.append(_make_batch(path, current))
                current, lines, body = [], 0, 0
            current.append(u)
            lines += u.changed_count
            body += u.body_lines
            if solo:
                batches.append(_make_batch(path, current))
                current, lines, body = [], 0, 0
        if current:
            batches.append(_make_batch(path, current))

    if preserve_order:
        # A triage pass has already ranked these; its order is the whole point.
        first_seen = {u.id: n for n, u in enumerate(units)}
        batches.sort(key=lambda b: min(first_seen[u.id] for u in b.units))
    else:
        batches.sort(key=lambda b: (b.is_test, -b.changed_count, b.files[0]))
    return batches


def _coupled_order(group: list[Unit]) -> list[Unit]:
    """A file's units in line order, except that call-linked units are kept adjacent.

    `batch_units` cuts a file's units into batches of four by position, so a private helper and
    its only caller could land in different agents when a third function sat between them. Two
    functions that call each other are exactly the pair one reviewer should see together, so the
    connected components of the same-file call graph are ordered as blocks (each block placed where
    its earliest member is), and line order applies within a block. This never crosses a file.
    """
    ids = {u.id for u in group}
    parent = {u.id: u.id for u in group}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u in group:
        for s in u.siblings:
            if s in ids:
                parent[root(u.id)] = root(s)
    blocks: dict[str, list[Unit]] = {}
    for u in group:
        blocks.setdefault(root(u.id), []).append(u)
    ordered: list[Unit] = []
    for block in sorted(blocks.values(), key=lambda b: min(u.start_line for u in b)):
        ordered.extend(sorted(block, key=lambda u: u.start_line))
    return ordered


# Beyond this many functions in one batch, the "review them one at a time" instruction is very
# likely not holding any more — this is the attention decay the whole pass exists to avoid. It is
# a GUESS, not a measurement: no experiment has been run to find where the decay actually starts.
# Treat it as the point at which the tool should warn, not as a known cliff.
BATCH_WARN_UNITS = 8


def fit_to_budget(units: list[Unit], budget: int, preserve_order: bool = False) -> list[Batch]:
    """Pack EVERY unit into at most `budget` batches, growing batches as needed.

    The alternative — `select_batches` — keeps batches small and drops the ones that do not fit,
    which reviews part of a change well. This reviews all of it less well. Neither is free, and
    which is right depends on how far the budget is from the natural batch count: close, and
    packing costs little; far, and it produces batches nobody can review attentively, at which
    point triaging a subset is the better use of the same agents.

    Files are kept together as long as possible and merged by directory adjacency only when the
    budget forces it, because a batch spanning unrelated modules is the original failure this
    pass was built to avoid.
    """
    by_file: dict[str, list[Unit]] = {}
    for u in units:
        by_file.setdefault(u.file, []).append(u)
    for group in by_file.values():
        group.sort(key=lambda u: u.start_line)

    # Order files by path so that merging, when it happens, joins neighbours in the tree.
    ordered = sorted(by_file.items(), key=lambda kv: kv[0])
    if preserve_order:
        first_seen = {u.file: n for n, u in reversed(list(enumerate(units)))}
        ordered = sorted(by_file.items(), key=lambda kv: first_seen[kv[0]])

    if budget <= 0 or len(ordered) <= 0:
        return [_batch_of(g) for _, g in ordered]

    # One batch per file is the floor that keeps batches file-pure. Under budget, spend the
    # remaining slots splitting the biggest batches back down.
    batches = [_batch_of(g) for _, g in ordered]
    if len(batches) <= budget:
        return _split_until(batches, budget, preserve_order)

    # Over budget: merge adjacent files into `budget` bins, balancing changed lines.
    total = sum(b.changed_count for b in batches) or 1
    target = total / budget
    bins: list[list[Unit]] = []
    current: list[Unit] = []
    running = 0
    for b in batches:
        if current and running + b.changed_count > target and len(bins) < budget - 1:
            bins.append(current)
            current, running = [], 0
        current.extend(b.units)
        running += b.changed_count
    if current:
        bins.append(current)
    return [_batch_of(g) for g in bins]


def _split_until(batches: list[Batch], budget: int, preserve_order: bool) -> list[Batch]:
    """Use leftover budget to break the largest batches down toward the ideal size."""
    while len(batches) < budget:
        biggest = max(batches, key=lambda b: (len(b.units), b.changed_count))
        if len(biggest.units) < 2:
            break
        half = len(biggest.units) // 2
        i = batches.index(biggest)
        batches[i : i + 1] = [_batch_of(biggest.units[:half]), _batch_of(biggest.units[half:])]
    if not preserve_order:
        batches.sort(key=lambda b: (b.is_test, -b.changed_count, b.files[0]))
    return batches


def batch_stats(batches: list[Batch]) -> dict:
    """Numbers the caller should report: how big the batches got, and whether to be worried."""
    if not batches:
        return {
            "batches": 0,
            "max_units": 0,
            "mean_units": 0.0,
            "oversized": 0,
            "spanning_files": 0,
        }
    sizes = [len(b.units) for b in batches]
    return {
        "batches": len(batches),
        "max_units": max(sizes),
        "mean_units": round(sum(sizes) / len(sizes), 1),
        "oversized": sum(1 for s in sizes if s > BATCH_WARN_UNITS),
        "spanning_files": sum(1 for b in batches if b.spans_files),
    }


def select_batches(
    batches: list[Batch], budget: int = MAX_BATCHES
) -> tuple[list[Batch], list[Batch]]:
    """Split batches into (selected, skipped) within the agent budget.

    Returns both halves rather than just the survivors so the caller can *report* what it did
    not review. A review that silently drops half a branch is worse than one that admits the
    cut, because the reader assumes coverage they did not get.
    """
    if budget <= 0 or len(batches) <= budget:
        return list(batches), []

    production = [b for b in batches if not b.is_test]
    tests = [b for b in batches if b.is_test]

    test_slots = 0
    if tests:
        test_slots = max(1, int(budget * TEST_BATCH_SHARE)) if budget >= 4 else 0
        test_slots = min(test_slots, len(tests))

    prod_slots = min(len(production), budget - test_slots)
    # Tests do not need the reservation if production leaves room to spare.
    test_slots = min(len(tests), budget - prod_slots)

    selected = production[:prod_slots] + tests[:test_slots]
    skipped = production[prod_slots:] + tests[test_slots:]
    selected.sort(key=lambda b: (b.is_test, -b.changed_count, b.files[0]))
    skipped.sort(key=lambda b: (b.is_test, -b.changed_count, b.files[0]))
    return selected, skipped


def read_lines_for(units: list[Unit]) -> int:
    """Lines an agent must read to judge these units, which must all be from ONE file.

    The unconditional "read the WHOLE file once" instruction is what made the original design
    expensive: one changed line in an 1,819-line test file cost a full-file read, and five
    batches of the same file each read all of it. A file that is mostly new *is* the change, so
    it gets read whole; anything else is read as changed regions plus surrounding context.
    """
    if not units:
        return 0
    file_lines = units[0].file_lines
    changed = sum(u.changed_count for u in units)
    if file_lines and changed / file_lines >= WHOLE_FILE_FRAC:
        return file_lines
    windows = sum(u.body_lines + CTX_LINES for u in units)
    return min(file_lines, windows) if file_lines else windows


def _mostly_new(units: list[Unit]) -> bool:
    file_lines = units[0].file_lines
    return bool(file_lines) and sum(u.changed_count for u in units) / file_lines >= WHOLE_FILE_FRAC


def _card(path: str, units: list[Unit], kind: str) -> Card:
    return Card(
        file=path,
        language=units[0].language,
        is_test=any(u.is_test for u in units),
        kind=kind,
        units=list(units),
        read_lines=read_lines_for(units),
    )


def _split_card(card: Card) -> list[Card]:
    """Break a card that is too large to read, but only when splitting actually reduces reading.

    Halving a mostly-new file's units does not help — both halves still read the whole file — so
    a big cohesive new artifact stays one review target and simply occupies its invocation alone.
    """
    if card.read_lines <= CARD_MAX_READ or len(card.units) < 2:
        return [card]
    half = len(card.units) // 2
    left = _card(card.file, card.units[:half], card.kind)
    right = _card(card.file, card.units[half:], card.kind)
    # Compare the WORST half, not the sum: splitting is about what one agent must read. On the
    # sum, a 26-region card reading 2,127 lines refused to split because both halves together
    # still read about as much, which was never the question.
    if max(left.read_lines, right.read_lines) >= card.read_lines * 0.9:
        return [card]
    return _split_card(left) + _split_card(right)


def _cohesive_groups(group: list[Unit]) -> list[tuple[str, list[Unit]]]:
    """A modified file's units grouped by the class they belong to, then the module-level rest."""
    owners: dict[str | None, list[Unit]] = {}
    for u in group:
        owners.setdefault(u.owner or None, []).append(u)
    out = [
        ("class", chunk)
        for owner, chunk in sorted(
            ((o, c) for o, c in owners.items() if o is not None),
            key=lambda kv: min(u.start_line for u in kv[1]),
        )
    ]
    residue = owners.get(None) or []
    if residue:
        # Free functions and out-of-function runs are one card for the file's module-level
        # change, not one per line run: call-linked ones belong together, and the rest are
        # already as cohesive as anything at module scope gets.
        out.append(("region", _coupled_order(residue)))
    return out


def make_cards(units: list[Unit]) -> list[Card]:
    """Group units into coherent review targets, ignoring cost entirely.

    Three rules, applied per file in this order, because each answers "what is the artifact
    here?" differently. A **test file is a suite**: fixture structure, shared setup, naming
    consistency and duplication between cases are suite-level properties, so a single test
    function is almost never the right craft boundary — this is where half the original
    invocations went. A file that is **mostly new** is one artifact to read whole; turning a
    352-line new test module into 46 units and twelve agents was the clearest symptom of
    conflating the review unit with the agent call. Anything else is grouped by **the class its
    changed methods belong to**, with module-level code as the residue.

    Cost is deliberately absent from every rule here. `pack_cards` owns cost.
    """
    by_file: dict[str, list[Unit]] = {}
    for u in units:
        by_file.setdefault(u.file, []).append(u)

    cards: list[Card] = []
    for path, group in by_file.items():
        group = sorted(group, key=lambda u: u.start_line)
        if group[0].is_test:
            cards.extend(_split_card(_card(path, group, "suite")))
        elif _mostly_new(group):
            cards.extend(_split_card(_card(path, group, "new-file")))
        else:
            for kind, chunk in _cohesive_groups(group):
                cards.extend(_split_card(_card(path, chunk, kind)))
    return cards


def pack_cards(
    cards: list[Card],
    max_read: int = INVOCATION_MAX_READ,
    max_cards: int = INVOCATION_MAX_CARDS,
    max_changed: int = INVOCATION_MAX_CHANGED,
    max_symbols: int = INVOCATION_MAX_SYMBOLS,
    preserve_order: bool = False,
) -> list[Batch]:
    """Fill agent invocations with cards, bounded by reading AND by semantic load.

    Packing is an execution optimisation and says nothing about the cards belonging together —
    the brief tells the agent so explicitly. That instruction is what makes crossing files safe
    here: the register shift the never-span-files rule protected against came from holding
    several files' worth of deep reading, not from glancing at six small independent targets.

    Both budgets matter. Lines dominate token cost, but the count of distinct targets is the
    better predictor of a reviewer losing depth, and having room in a context window is not the
    same as using all of it evenly. A card that busts a budget on its own still gets its own
    invocation rather than being dropped.
    """
    if not cards:
        return []
    ordered = (
        list(cards)
        if preserve_order
        else sorted(
            cards,
            key=lambda c: (
                c.is_test,
                str(Path(c.file).parent),
                c.file,
                min(u.start_line for u in c.units),
            ),
        )
    )

    groups: list[list[Card]] = []
    current: list[Card] = []

    def fits(card: Card) -> bool:
        if not current:
            return True
        # Production and test cards stay in separate invocations: the agent budget reserves
        # slots for tests by `is_test`, and a mixed invocation makes that reservation and the
        # reporting split meaningless. Pairing a class with the suite that covers it is a real
        # idea and a separate change.
        if current[0].is_test != card.is_test:
            return False
        return (
            sum(c.read_lines for c in current) + card.read_lines <= max_read
            and len(current) + 1 <= max_cards
            and sum(c.changed_count for c in current) + card.changed_count <= max_changed
            and sum(len(c.units) for c in current) + len(card.units) <= max_symbols
        )

    for card in ordered:
        if not fits(card):
            groups.append(current)
            current = []
        current.append(card)
    if current:
        groups.append(current)
    return [
        _batch_of_cards(g) for g in _topup(groups, max_read, max_cards, max_changed, max_symbols)
    ]


def _room(
    group: list[Card], max_read: int, max_cards: int, max_changed: int, max_symbols: int
) -> tuple[int, int, int, int]:
    return (
        max_read - sum(c.read_lines for c in group),
        max_cards - len(group),
        max_changed - sum(c.changed_count for c in group),
        max_symbols - sum(len(c.units) for c in group),
    )


def _topup(
    groups: list[list[Card]], max_read: int, max_cards: int, max_changed: int, max_symbols: int
) -> list[list[Card]]:
    """Merge part-filled invocations left behind by the single greedy pass.

    Packing in directory order gives each agent cards that at least sit near each other, but it
    cuts an invocation as soon as the next card does not fit and never revisits it. On
    a large reference MR that left invocations of 17, 34 and 48 lines beside others near the
    600-line budget — agents paying full charter cost for almost no code. This folds the
    smallest groups into whichever remaining group has the least room to spare, so affinity
    still decides the bulk of the packing and only the stragglers move.
    """
    remaining = sorted(groups, key=lambda g: sum(c.read_lines for c in g))
    merged: list[list[Card]] = []
    while remaining:
        group = remaining.pop(0)
        need = (
            sum(c.read_lines for c in group),
            len(group),
            sum(c.changed_count for c in group),
            sum(len(c.units) for c in group),
        )
        hosts = [
            h
            for h in remaining
            if h[0].is_test == group[0].is_test
            and all(
                r >= n
                for r, n in zip(
                    _room(h, max_read, max_cards, max_changed, max_symbols), need, strict=True
                )
            )
        ]
        if hosts:
            # Best fit: the host with the least leftover reading, so slack is not spread thin.
            host = min(
                hosts, key=lambda h: _room(h, max_read, max_cards, max_changed, max_symbols)[0]
            )
            host.extend(group)
            remaining.sort(key=lambda g: sum(c.read_lines for c in g))
        else:
            merged.append(group)
    return merged


def fit_cards(cards: list[Card], budget: int, preserve_order: bool = False) -> list[Batch]:
    """Pack every card into at most `budget` invocations, relaxing the budgets until it fits.

    The counterpart to dropping the overflow: this reviews all of the change less well, where
    `select_batches` reviews part of it well. Which is right depends on how far the budget is
    from the natural invocation count — close, and relaxing costs little; a fraction of it, and
    the result is invocations nobody can review attentively.

    It cannot go below one invocation for production and one for tests, because they are never
    mixed; a budget of 1 on a change with both returns 2.
    """
    if not cards:
        return []
    if budget <= 0:
        return pack_cards(cards, preserve_order=preserve_order)
    scale = 1
    packed = pack_cards(cards, preserve_order=preserve_order)
    while len(packed) > budget and scale <= 64:
        scale *= 2
        packed = pack_cards(
            cards,
            max_read=INVOCATION_MAX_READ * scale,
            max_cards=INVOCATION_MAX_CARDS * scale,
            max_changed=INVOCATION_MAX_CHANGED * scale,
            max_symbols=INVOCATION_MAX_SYMBOLS * scale,
            preserve_order=preserve_order,
        )
    return packed


def _batch_of_cards(cards: list[Card]) -> Batch:
    files: list[str] = []
    for c in cards:
        if c.file not in files:
            files.append(c.file)
    return Batch(
        files=files,
        language=cards[0].language,
        is_test=any(c.is_test for c in cards),
        changed_count=sum(c.changed_count for c in cards),
        units=[u for c in cards for u in c.units],
        cards=list(cards),
    )


def card_stats(batches: list[Batch]) -> dict:
    """What the caller must report: how loaded the invocations got, and how much reading it costs."""
    if not batches:
        return {
            "invocations": 0,
            "cards": 0,
            "max_cards": 0,
            "mean_cards": 0.0,
            "read_lines": 0,
            "max_read": 0,
            "crowded": 0,
        }
    per = [len(b.cards) or 1 for b in batches]
    reads = [b.read_lines for b in batches]
    # Changed functions per card, counted only on cards that are a *slice* of a file. On a whole
    # artifact — a new file, a test suite — the artifact is the review target, so its 46 test
    # cases are not 46 things competing for attention and must not be reported as crowding.
    sliced = [len(c.units) for b in batches for c in b.cards if c.kind in ("class", "region")]
    return {
        "invocations": len(batches),
        "cards": sum(per),
        "max_cards": max(per),
        "mean_cards": round(sum(per) / len(per), 1),
        "read_lines": sum(reads),
        "max_read": max(reads),
        "crowded": sum(1 for p in per if p > CARD_WARN_PER_INVOCATION),
        "max_sliced_units": max(sliced, default=0),
        "oversized_cards": sum(1 for s in sliced if s > BATCH_WARN_UNITS),
    }


def card_as_json(card: Card) -> dict:
    return {
        "title": card.title,
        "file": card.file,
        "kind": card.kind,
        "language": card.language,
        "is_test": card.is_test,
        "changed_count": card.changed_count,
        "read_lines": card.read_lines,
        "risk": card.risk,
        "tags": card.tags,
        "lenses": card.lenses,
        "units": [as_json(u) for u in card.units],
    }


def as_json(unit: Unit) -> dict:
    """A unit as JSON, with its derived fields — `asdict` skips properties."""
    return {
        "id": unit.id,
        "body_lines": unit.body_lines,
        "untested": unit.untested,
        "lenses": unit.lenses,
        **asdict(unit),
    }


def _common_dir(paths: list[str]) -> str:
    """The deepest directory all these paths share, for naming a multi-file batch."""
    parts = [Path(p).parent.parts for p in paths]
    shared: list[str] = []
    # Not strict: stopping at the shallowest path is what makes this the *shared* prefix.
    for chunk in zip(*parts, strict=False):
        if len(set(chunk)) != 1:
            break
        shared.append(chunk[0])
    return "/".join(shared) or "."


def _make_batch(path: str, units: list[Unit]) -> Batch:
    return _batch_of(units)


def _batch_of(units: list[Unit]) -> Batch:
    files: list[str] = []
    for u in units:
        if u.file not in files:
            files.append(u.file)
    return Batch(
        files=files,
        language=units[0].language,
        is_test=any(u.is_test for u in units),
        changed_count=sum(u.changed_count for u in units),
        units=list(units),
    )


def batch_as_json(batch: Batch) -> dict:
    return {
        "label": batch.label,
        "files": batch.files,
        "spans_files": batch.spans_files,
        "language": batch.language,
        "is_test": batch.is_test,
        "changed_count": batch.changed_count,
        "body_lines": batch.body_lines,
        "read_lines": batch.read_lines,
        "risk": batch.risk,
        "tags": batch.tags,
        "lenses": batch.lenses,
        "cards": [card_as_json(c) for c in batch.cards],
        "units": [as_json(u) for u in batch.units],
    }


def select_by_id(units: list[Unit], ids: list[str]) -> tuple[list[Unit], list[str]]:
    """Keep the named units, in the order given, and report ids that matched nothing.

    Order is the caller's, not ours: the point of `--select` is that a triage pass has ranked
    these by risk, and re-sorting them by line count here would throw that judgement away.
    """
    by_id = {u.id: u for u in units}
    kept = [by_id[i] for i in ids if i in by_id]
    unknown = [i for i in ids if i not in by_id]
    return kept, unknown


# --- change context: computation ------------------------------------------------------


def _is_plumbing(line: str) -> bool:
    """A line that declares, imports, annotates or comments, and decides nothing."""
    if _TRIVIAL.match(line) or _COMMENT_LINE.match(line):
        return True
    if _STATEMENT_START.match(line):
        return False
    if _IMPORT_LINE.match(line) or _ANNOTATION_LINE.match(line):
        return True
    # A declaration is plumbing only when nothing is *called* in it: `Foo x = build(y)` decides.
    if "(" in line and not re.search(r"new\s+[\w$<>.]+\(\s*\)", line):
        return False
    return bool(_DECL_TYPED.match(line) or _DECL_ANNOTATED.match(line))


def _substitutions(removed: list[str], added: list[str]) -> list[tuple[str, str]] | None:
    """Identifier pairs that differ between two equal-length line lists.

    None when the edit is more than identifier substitution — a different token count, or a
    changed operator, literal or keyword — because then something other than a name changed.
    """
    if len(removed) != len(added):
        return None
    subs: list[tuple[str, str]] = []
    for old, new in zip(removed, added, strict=True):
        old_tokens, new_tokens = _TOKEN.findall(old), _TOKEN.findall(new)
        if len(old_tokens) != len(new_tokens):
            return None
        for a, b in zip(old_tokens, new_tokens, strict=True):
            if a == b:
                continue
            if (
                not (_IDENT.fullmatch(a) and _IDENT.fullmatch(b))
                or a in _KEYWORDS
                or b in _KEYWORDS
            ):
                return None
            subs.append((a, b))
    return subs


def _rename_sites(hunks: list[Hunk]) -> dict[tuple[str, str], int]:
    """Identifier substitutions the change applies at enough sites to count as a rename."""
    counts: dict[tuple[str, str], int] = {}
    for h in hunks:
        for pair in _substitutions(h.removed, h.added) or []:
            counts[pair] = counts.get(pair, 0) + 1
    return {pair: n for pair, n in counts.items() if n >= _RENAME_MIN_SITES}


def _triviality(hunks: list[Hunk], renames: dict[tuple[str, str], int]) -> str | None:
    """Why this unit's edit is mechanical, or None when it has substance.

    A hunk that both removes and adds is trivial only as a rename, never as "plumbing on both
    sides": `int max = 5` becoming `int max = 7` is two declarations and a changed decision.
    """
    if not hunks:
        return None
    reasons: set[str] = set()
    for h in hunks:
        if h.removed and h.added:
            subs = _substitutions(h.removed, h.added)
            if not subs or any(pair not in renames for pair in subs):
                return None
            reasons.add("rename " + ", ".join(sorted({f"{a}→{b}" for a, b in subs})))
        else:
            if not all(_is_plumbing(line) for line in (h.added or h.removed)):
                return None
            reasons.add("imports, declarations, annotations or comments only")
    return "; ".join(sorted(reasons))


def _tags_for(unit: Unit, source: list[str]) -> list[str]:
    """Risk tags from the lines this unit changed plus its signature — not its whole body."""
    changed = [source[n - 1] for n in unit.changed_lines if 1 <= n <= len(source)]
    head = source[unit.start_line - 1 : min(unit.end_line, unit.start_line + 2)]
    text = "\n".join(changed + head)
    return [name for name, pattern in _TAG_RULES if pattern.search(text)]


# Names too common to grep for: the hits would be every class's `run`, not this unit's callers.
_GENERIC_NAMES = frozenset(
    {
        "main",
        "run",
        "get",
        "set",
        "init",
        "setup",
        "test",
        "apply",
        "call",
        "execute",
        "handle",
        "process",
        "update",
        "create",
        "delete",
        "build",
        "start",
        "stop",
        "close",
        "open",
        "read",
        "write",
        "toString",
        "equals",
        "hashCode",
        "constructor",
        "render",
        "ngOnInit",
        "ngOnDestroy",
        "ngOnChanges",
        "map",
        "filter",
        "reduce",
        "validate",
        "parse",
        "format",
        "save",
        "load",
        "send",
    }
)
_DEF_LINE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|override|open|export|async|default"
    r"|declare|synchronized|native|suspend|inline)\s+)*"
    r"(?:(?:def|function|fun|func|sub)\s+[\w$]+\s*\("
    r"|[\w$<>\[\],.?]+\s+[\w$]+\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{?\s*$)"
)


def _grep_callers(repo: Path, unit: Unit, ref: str | None, prefix: str = "") -> list[str]:
    """`file:line` of every call site of this unit outside its own body, via one `git grep`.

    `prefix` labels hits from a repository other than the one the change lives in, so a
    finding can say which repo the caller is in; everything downstream splits the line number
    off the end and is unaffected by it.

    Name-based and therefore approximate — an overload or a same-named method on another class
    counts — which is why the reviewer is told these are leads to verify, not facts to cite.

    Grepped at `ref` when the scope is a committed range, for the same reason the file contents
    are read there: the working tree may be a different branch entirely, and callers found in it
    would describe somebody else's code.
    """
    name = unit.simple_name
    if not name or len(name) < 3 or name in _GENERIC_NAMES or name.startswith("__"):
        return []
    # POSIX classes, not `\b` or `\s`: git grep's -E is a POSIX ERE and silently matches nothing
    # on Perl escapes, which is how every unit briefly reported zero callers.
    pattern = rf"(^|[^[:alnum:]_]){re.escape(name)}[[:space:]]*\("
    try:
        if ref:
            out = _git(repo, "grep", "-I", "-n", "-E", pattern, ref)
        else:
            out = _git(repo, "grep", "-I", "-n", "--untracked", "-E", pattern)
    except RuntimeError:
        return []  # no matches
    hits: list[str] = []
    for line in out.splitlines():
        if ref and line.startswith(f"{ref}:"):
            line = line[len(ref) + 1 :]  # `git grep <ref>` prefixes every hit with the ref
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        if not number.isdigit() or _reviewable(path) is None:
            continue  # prose that mentions the function is not a caller
        n = int(number)
        if not prefix and path == unit.file and unit.start_line <= n <= unit.end_line:
            continue
        if _is_definition(text):
            continue
        hits.append(f"{prefix}{path}:{n}")
    return hits


def _callers(
    repo: Path, unit: Unit, ref: str | None = None, siblings: Sequence[Sibling] = ()
) -> list[str]:
    """Call sites across the whole repository set, the change's own repo first.

    A change that splits across repositories is what this exists for: the producer's signature
    has no callers in its own repo and so reads as safe to change, while every real caller sits
    in the consumer. Siblings are grepped in their working tree rather than at `ref`, because
    each repo carries the branch on its own checkout.
    """
    hits = _grep_callers(repo, unit, ref)
    for sib in siblings:
        hits.extend(_grep_callers(sib.path, unit, None, prefix=f"{sib.label}:"))
    return hits


def _is_definition(text: str) -> bool:
    """A line that declares a function of this name, as opposed to calling it.

    `return compute_total(items)` in Python has no trailing semicolon, so on shape alone it looks
    like `Type name(args)`; a statement keyword in the type position is the tell.
    """
    return not _STATEMENT_START.match(text) and bool(_DEF_LINE.match(text))


def _nests(outer: Unit, inner: Unit) -> bool:
    return (
        outer.file == inner.file
        and outer.start_line <= inner.start_line
        and inner.end_line <= outer.end_line
    )


def _link_units(units: list[Unit], sources: dict[str, list[str]]) -> None:
    """Record which units in the change call which, on both ends (`siblings`), and note test units
    that exercise a production unit (`tests_in_change`)."""
    named = [
        (u, re.compile(rf"\b{re.escape(u.simple_name)}\s*\("))
        for u in units
        if u.simple_name and len(u.simple_name) >= 3 and u.simple_name not in _GENERIC_NAMES
    ]
    for caller in units:
        src = sources.get(caller.file)
        if not src:
            continue
        body = "\n".join(src[caller.start_line - 1 : caller.end_line])
        for callee, pattern in named:
            if callee is caller or _nests(caller, callee) or _nests(callee, caller):
                continue
            if not pattern.search(body):
                continue
            if callee.id not in caller.siblings:
                caller.siblings.append(callee.id)
            if caller.id not in callee.siblings:
                callee.siblings.append(caller.id)
            if caller.is_test and not callee.is_test and caller.id not in callee.tests_in_change:
                callee.tests_in_change.append(caller.id)


def enrich(
    repo: Path,
    scope: list[str],
    units: list[Unit],
    sources: dict[str, list[str]],
    hunks: list[Hunk],
    read_ref: str | None = None,
    siblings: Sequence[Sibling] = (),
) -> None:
    """Attach the change context to every unit: triviality, tags, callers, coupling, risk."""
    renames = _rename_sites(hunks)
    for u in units:
        reason = _triviality(_hunks_touching(_hunks(repo, scope, u.file), u), renames)
        if reason:
            u.trivial, u.trivial_reason = True, reason
        u.tags = _tags_for(u, sources[u.file])
    _link_units(units, sources)
    for u in units:
        if u.is_test or u.trivial:
            continue  # callers matter for production code someone might break
        hits = _callers(repo, u, read_ref, siblings)
        u.caller_count = len(hits)
        u.callers = hits[:20]
        u.callers_in_tests = sum(1 for h in hits if _TEST_PATH.search(h.rsplit(":", 1)[0]))
        u.callers_in_siblings = sum(
            1 for h in hits for sib in siblings if h.startswith(f"{sib.label}:")
        )
        signature_changed = any(n <= u.start_line + 2 for n in u.changed_lines)
        if signature_changed and (u.caller_count - u.callers_in_tests) >= 3:
            u.tags.append("public_api")
        u.risk = "high" if _HIGH_RISK_TAGS.intersection(u.tags) else "normal"


def call_links(units: list[Unit]) -> list[list[str]]:
    """Every coupled pair once, as [a, b] with a < b."""
    seen: set[tuple[str, str]] = set()
    for u in units:
        for s in u.siblings:
            seen.add((min(u.id, s), max(u.id, s)))
    return [list(p) for p in sorted(seen)]


def change_signals(
    repo: Path, scope: list[str], units: list[Unit], siblings: Sequence[Sibling] = ()
) -> dict[str, list[str]]:
    """Structural facts that tell the skill which wide passes have something to look at.

    Each list is a set of reasons; an empty list means the pass can be skipped and the report
    can say why. `architect` wants shape to look at, `integration` wants more than one thing that
    has to agree, `maker` wants a decision that could have gone another way.
    """
    substantive = [u for u in units if not u.trivial]
    prod_files = sorted({u.file for u in substantive if not u.is_test})
    architect: list[str] = []
    integration: list[str] = []
    maker: list[str] = []

    if len(prod_files) > 1:
        architect.append(f"{len(prod_files)} production files changed")
        integration.append(f"{len(prod_files)} production files changed")
    try:
        added_files = [
            p
            for p in _git(repo, "diff", "--name-only", "--diff-filter=A", *scope).splitlines()
            if p.strip()
        ]
    except RuntimeError:
        added_files = []
    for path in added_files:
        full = repo / path
        if _reviewable(path) and full.is_file() and not _TEST_PATH.search(path):
            text = full.read_text(encoding="utf-8", errors="replace")
            if any(_CONTAINER.match(line) for line in text.splitlines()):
                architect.append(f"new file declares a class or interface: {path}")

    for path in prod_files:
        added = "\n".join(line for h in _hunks(repo, scope, path) for line in h.added)
        fired = [name for name, pattern in _DECISION_SIGNALS if pattern.search(added)]
        if _DECISION_PATH.search(path):
            fired.append("migration-shaped path")
        for name in fired:
            maker.append(f"{name}: {path}")
            architect.append(f"{name}: {path}")

    untested: list[str] = []
    for u in substantive:
        if u.is_test:
            continue
        who = u.symbol or f"lines {u.start_line}-{u.end_line} of {u.file}"
        if "public_api" in u.tags:
            architect.append(f"signature of {who} changed; {u.caller_count} call sites")
            integration.append(f"signature of {who} changed; {u.caller_count} call sites")
        for tag in ("boundary", "persistence", "concurrency"):
            if tag in u.tags:
                integration.append(f"{tag}: {who}")
        if u.untested:
            untested.append(who)
    if untested:
        shown = ", ".join(untested[:6]) + (
            f" and {len(untested) - 6} more" if len(untested) > 6 else ""
        )
        integration.append(
            f"{len(untested)} changed function(s) with no test in the change or the repo: {shown}"
        )
    crossing = [
        pair for pair in call_links(units) if pair[0].rsplit(":", 1)[0] != pair[1].rsplit(":", 1)[0]
    ]
    if crossing:
        integration.append(f"{len(crossing)} call link(s) between changed functions cross files")
    # A caller in another repository is the one contract this run cannot verify by reading its own
    # tree, so it is named per repo rather than folded into the count above: the seam is where a
    # split change breaks, and each per-repo review reports clean while it does.
    for sib in siblings:
        reached = [u for u in substantive if not u.is_test and u.callers_in_siblings]
        in_sib = [u for u in reached if any(h.startswith(f"{sib.label}:") for h in u.callers)]
        if in_sib:
            who = ", ".join(sorted({u.symbol or u.file for u in in_sib})[:6])
            integration.append(f"{len(in_sib)} changed function(s) called from {sib.label}: {who}")
            architect.append(f"the change crosses the boundary into {sib.label}")

    def dedupe(xs: list[str]) -> list[str]:
        """Keep the first occurrence of each signal, drop repeats."""
        return list(dict.fromkeys(xs))

    return {
        "architect": dedupe(architect),
        "integration": dedupe(integration),
        "maker": dedupe(maker),
    }


def refill(
    selected: list[Unit],
    pool: list[Unit],
    budget: int,
    max_units: int,
    max_lines: int,
    pack: Callable[[list[Unit]], list[Batch]] | None = None,
) -> tuple[list[Unit], list[Batch], list[str]]:
    """Spend agents a triage selection left idle on the next-best units, in the order given.

    A selection of ten units in three files costs three agents; the other seven were budgeted and
    would otherwise go unspent while deprioritised units went unreviewed. Candidates are tried in
    order and kept only when the invocation count stays within budget — a unit from a file
    already in the plan is often free, one from a new file may cost an agent.

    `pack` is how the caller turns units into invocations, and it must be the *same* packer the
    run will actually use: counting with a denser packer than the run uses under-spends the
    budget, which is the failure this function exists to fix.
    """

    def default_pack(us: list[Unit]) -> list[Batch]:
        return batch_units(us, max_units, max_lines, preserve_order=True)

    packer = pack or default_pack
    units = list(selected)
    batches = packer(units)
    added: list[str] = []
    for candidate in pool:
        trial = packer(units + [candidate])
        if len(trial) <= budget:
            units.append(candidate)
            batches = trial
            added.append(candidate.id)
    return units, batches, added


def _ranges(numbers: list[int]) -> str:
    """`[3,4,5,9,12,13]` -> `3-5, 9, 12-13`. Strictly contiguous runs, unlike `_group_runs`."""
    nums = sorted(set(numbers))
    out: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def write_briefs(
    out_dir: Path,
    label: str,
    scope: list[str],
    units: list[Unit],
    batches: list[Batch],
    signals: dict[str, list[str]],
    siblings: Sequence[Sibling] = (),
) -> dict:
    """Write the change context and one brief per batch, so the orchestrator hands agents a path
    instead of transcribing unit lists into prompts by hand.

    Everything written here is fact: spans, changed lines, tags, call sites, coupling. No agent's
    findings and no reasoning about what is wrong — that would anchor the reviewers this exists to
    keep independent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    by_id = {u.id: u for u in units}
    substantive = [u for u in units if not u.trivial]
    trivial = [u for u in units if u.trivial]

    def name(u: Unit) -> str:
        return u.symbol or f"(outside any function, lines {u.start_line}-{u.end_line})"

    where = "the repository set" if siblings else "the repository"
    lines = [
        f"CHANGE CONTEXT — {label}",
        f"diff arguments: {' '.join(scope)}",
        "",
        f"These are facts computed from the diff and {where}, not findings. Call sites are",
        "name-based leads: verify before you cite one.",
    ]
    if siblings:
        # The reviewer has to be told the set exists, or a labelled call site reads as a path.
        lines += [
            "",
            "REPOSITORIES — the diff is this repo's; callers and tests were searched in all of them",
            "  this repo: (call sites here are written `path:line`, unlabelled)",
        ]
        lines += [f"  {s.label}: {s.path}  (written `{s.label}:path:line`)" for s in siblings]
    lines += ["", "FILES"]
    for kind, sel in (("production", lambda u: not u.is_test), ("test", lambda u: u.is_test)):
        files = sorted({u.file for u in substantive if sel(u)})
        lines.append(f"  {kind}: " + (", ".join(files) if files else "none"))

    # The change map: orientation, so an agent reviewing one card still knows the shape of the
    # change it sits in. Facts only — symbols, spans and which changed function calls which.
    # Independence of judgement is the point of running these agents separately; isolation from
    # factual context was never the point, and withholding the map just made each reviewer guess
    # at what the rest of the change was doing.
    lines += ["", "CHANGE MAP — the shape of this change, for orientation only"]
    for kind, sel in (("production", lambda u: not u.is_test), ("test", lambda u: u.is_test)):
        group = [u for u in substantive if sel(u)]
        if not group:
            continue
        lines.append(f"  {kind}/")
        for path in sorted({u.file for u in group}):
            lines.append(f"    {path}")
            for u in sorted((x for x in group if x.file == path), key=lambda x: x.start_line):
                callees = {by_id[s].symbol or s for s in u.siblings if s in by_id}
                arrow = ("  ->  " + ", ".join(sorted(callees)[:4])) if callees else ""
                lines.append(f"      {name(u)}  [{u.start_line}-{u.end_line}]{arrow}")

    lines += ["", "CHANGED FUNCTIONS (substantive)"]
    for u in substantive:
        extra = []
        if u.tags:
            extra.append("tags=" + ",".join(u.tags))
        if not u.is_test and u.simple_name:
            extra.append(
                f"callers={u.caller_count - u.callers_in_tests} prod/{u.callers_in_tests} test"
            )
        if u.tests_in_change:
            extra.append(f"tested-in-change={len(u.tests_in_change)}")
        elif u.untested:
            extra.append("untested")
        if u.removed_count:
            extra.append(f"removed={u.removed_count}")
        lines.append(
            f"  [{u.risk:6}] {u.file}  {name(u)}  [{u.start_line}-{u.end_line}]"
            f"  changed {_ranges(u.changed_lines)}  {' '.join(extra)}".rstrip()
        )
    if trivial:
        lines += ["", "MECHANICAL EDITS (not reviewed unless asked)"]
        for u in trivial:
            lines.append(f"  {u.file}  {name(u)}  — {u.trivial_reason}")
    links = call_links(units)
    if links:
        lines += ["", "CALL LINKS BETWEEN CHANGED FUNCTIONS"]
        for a, b in links:
            lines.append(f"  {name(by_id[a])} ({a})  <->  {name(by_id[b])} ({b})")
    lines += ["", "SIGNALS"]
    for pass_name in ("architect", "integration", "maker"):
        reasons = signals.get(pass_name) or []
        lines.append(f"  {pass_name}: " + ("; ".join(reasons) if reasons else "none"))
    context_path = out_dir / "context.txt"
    context_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    brief_paths: list[str] = []
    for n, batch in enumerate(batches, 1):
        cards = batch.cards or [
            _card(f, [u for u in batch.units if u.file == f], "region") for f in batch.files
        ]
        brief = [
            f"REVIEW BRIEF {n} of {len(batches)}",
            f"Review cards in this brief: {len(cards)}",
            f"Lines to read:  {batch.read_lines}",
            f"Language:       {batch.language}",
            "Lens files to read in addition to your charter: "
            + (", ".join(batch.lenses) if batch.lenses else "none"),
            "",
            "HOW TO USE THIS BRIEF",
            "",
            "Each card below is an INDEPENDENT review target. Review them one at a time and",
            "finish one before starting the next. Cards were scheduled into the same agent",
            "because they fit a budget together — that is all it means. Do NOT infer that two",
            "cards form one design unit, look for a relationship between them, or report a",
            "finding that spans them: cross-file behaviour and whole-change shape belong to the",
            "integration and architecture passes and are covered there.",
            "",
            "Read what each card tells you to read. A card marked `whole file` is mostly new,",
            "so read all of it; otherwise read the listed spans plus enough surrounding code to",
            "judge them, not the entire file.",
            "",
        ]
        for i, c in enumerate(cards, 1):
            whole = c.read_lines >= (c.units[0].file_lines or 0) and c.units[0].file_lines
            brief += [
                f"--- CARD {i} of {len(cards)} " + "-" * 52,
                f"Target:  {c.title}",
                f"File:    {c.file}",
                f"Kind:    {c.kind}"
                + {
                    "new-file": "  (a new artifact — judge it as a whole)",
                    "suite": "  (a test suite — fixtures, setup, naming and duplication between"
                    " cases are the review, not each case in isolation)",
                    "class": "  (the changed methods of one class)",
                    "region": "  (changed regions at module level)",
                }.get(c.kind, ""),
                f"Risk:    {c.risk}",
                "Read:    "
                + (
                    "the whole file"
                    if whole
                    else f"the spans below plus surrounding context (~{c.read_lines} lines)"
                ),
                "",
                "Changed in this card:",
            ]
            for u in c.units:
                brief.append(
                    f"  · {name(u)} — span {u.start_line}-{u.end_line}, "
                    f"changed lines {_ranges(u.changed_lines)}"
                    + (f", {u.removed_count} line(s) deleted here" if u.removed_count else "")
                )
                if u.tags:
                    brief.append(f"    touches: {', '.join(u.tags)}")
                if u.callers:
                    more = (
                        f" (+{u.caller_count - len(u.callers[:5])} more)"
                        if u.caller_count > 5
                        else ""
                    )
                    brief.append(
                        f"    call sites to check (leads, not facts): {', '.join(u.callers[:5])}{more}"
                    )
                coupled = [by_id[s] for s in u.siblings if s in by_id]
                if coupled:
                    brief.append(
                        "    also changed and coupled to this: "
                        + ", ".join(f"{name(c)} ({c.file})" for c in coupled)
                    )
                if not u.is_test:
                    if u.tests_in_change:
                        brief.append(
                            "    exercised by changed tests: "
                            + ", ".join(name(by_id[t]) for t in u.tests_in_change if t in by_id)
                        )
                    elif u.untested:
                        brief.append(
                            "    no test in the change or the repository names this function"
                        )
            brief.append("")
        brief_path = out_dir / f"batch-{n}.txt"
        brief_path.write_text("\n".join(brief) + "\n", encoding="utf-8")
        brief_paths.append(str(brief_path))
    return {"context": str(context_path), "batches": brief_paths}


def _file_text(repo: Path, path: str, ref: str | None) -> str | None:
    """The new-side content of one file, from a ref when the scope names one.

    Read this from the working tree and the unit list silently becomes wrong whenever the
    checkout does not match the range: on a committed range whose files are absent from the
    current branch, every span that cannot be parsed simply disappears. Measured on
    a large reference MR, the same `--base X --head Y` produced 61 units against an unrelated
    checkout and 207 against the range's own tree, with no warning either time.
    """
    if ref:
        try:
            return _git(repo, "show", f"{ref}:{path}")
        except RuntimeError:
            return None  # added on the other side, or deleted at this ref
    full = repo / path
    return full.read_text(encoding="utf-8", errors="replace") if full.is_file() else None


def collect(
    repo: Path,
    scope: list[str],
    only: list[str] | None,
    context: bool = True,
    read_ref: str | None = None,
    siblings: Sequence[Sibling] = (),
) -> list[Unit]:
    """Every review unit in the scope, with its change context attached unless `context` is off.

    `read_ref` is the ref to read file contents from; None means the working tree, which is
    right for every scope that ends at the working tree and wrong for a committed range.
    """
    files = only or _changed_files(repo, scope)
    units: list[Unit] = []
    sources: dict[str, list[str]] = {}
    hunks: list[Hunk] = []
    for path in files:
        language = _reviewable(path)
        if language is None:
            continue
        changed = _changed_lines(repo, scope, path)
        if not changed:
            continue
        text = _file_text(repo, path, read_ref)
        if text is None:
            continue
        file_hunks = _hunks(repo, scope, path)
        sources[path] = text.splitlines()
        hunks.extend(file_hunks)
        units.extend(units_for_file(path, language, text, changed, file_hunks))
    if context and units:
        enrich(repo, scope, units, sources, hunks, read_ref, siblings)
    # Production before tests, then risk, then by volume. Volume is an imperfect proxy for review
    # value — a new 30-line function ranks above a 3-line change to a pricing rule — which is why
    # risk comes first and the triage pass exists at all; within one class it at least tracks how
    # much new code there is to read.
    units.sort(key=lambda u: (u.is_test, u.risk != "high", -u.changed_count, u.file, u.start_line))
    return units


# --- self-test ------------------------------------------------------------------------

_JAVA_FIXTURE = """\
package com.example;

public class OrderService {
    private final Repo repo;

    public void book(String id) {
        if (id == null) {
            throw new IllegalArgumentException("nope { not a brace");
        }
        repo.save(id);
    }

    public int total(List<Item> items) {
        return items.size();
    }
}
"""

_PY_FIXTURE = """\
import os


class Planner:
    def plan(self, items):
        def inner(x):
            return x + 1
        return [inner(i) for i in items]

    def total(self):
        return 0


def free_function():
    return "{"
"""


_JAVA_ANNOTATED = """\
public class Svc {
    @Override
    @Transactional
    public void run() {
        work();
    }
}
"""

_PY_DECORATED = """\
class S:
    @property
    def value(self):
        return 1
"""


_TS_SPEC = """\
describe('OrderService', () => {
  let service: OrderService;

  beforeEach(() => {
    service = new OrderService();
  });

  it('places an order', () => {
    expect(service.book('1')).toBeTruthy();
  });
});
"""

_TS_NESTED = """\
describe('Grid', () => {
  describe('Tags', () => {
    it('adds a tag', () => {
      expect(1).toBe(1);
    });
  });

  describe('Sorting', () => {
    it('sorts by name', () => {
      expect(2).toBe(2);
    });
  });
});
"""

_TS_EMPTY_CLASS = """\
class MockService extends Real {}

describe('Component', () => {
  it('works', () => {
    expect(1).toBe(1);
  });
});
"""

_TS_MODULE = """\
router.get('/orders', (req, res) => {
  res.send(list());
});

export function list() {
  return [];
}
"""

_JAVA_LAMBDA = """\
public class Svc {
    public void run(List<Order> orders) {
        orders.forEach(order -> {
            order.touch();
        });
    }

    private java.util.List<String> names(Map<String, Object> in) {
        return List.of();
    }
}
"""


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    java = _brace_spans(_JAVA_FIXTURE)
    names = {n: (s, e) for n, s, e in java}
    check("java: book found", "OrderService.book" in names, True)
    check("java: total found", "OrderService.total" in names, True)
    check("java: book span", names.get("OrderService.book"), (6, 11))
    check("java: total span", names.get("OrderService.total"), (13, 15))

    py = {n: (s, e) for n, s, e in _python_spans(_PY_FIXTURE)}
    check("py: method found", "Planner.plan" in py, True)
    check("py: nested found", "Planner.plan.inner" in py, True)
    check("py: free function", py.get("free_function"), (14, 15))

    # A change inside the nested function is attributed to the nested function, not the method.
    nested = units_for_file("p.py", "python", _PY_FIXTURE, [7])
    check("py: innermost wins", [u.symbol for u in nested], ["Planner.plan.inner"])

    # A change at class-body level has no enclosing function and must still be reported.
    orphan = units_for_file("C.java", "java", _JAVA_FIXTURE, [4])
    check("java: orphan reported", [u.symbol for u in orphan], [None])

    # Two functions changed in one file produce two units, ordered by change volume.
    two = units_for_file("C.java", "java", _JAVA_FIXTURE, [7, 8, 9, 14])
    check(
        "java: two units",
        [u.symbol for u in two],
        ["OrderService.book", "OrderService.total"],
    )

    # A brace inside a string literal must not close the body early.
    check("java: string brace ignored", names["OrderService.book"][1], 11)

    # Annotations and decorators belong to the method they govern, not to the space above it.
    annotated = {n: (s, e) for n, s, e in _brace_spans(_JAVA_ANNOTATED)}
    check("java: annotations absorbed", annotated.get("Svc.run"), (2, 6))
    decorated = {n: (s, e) for n, s, e in _python_spans(_PY_DECORATED)}
    check("py: decorator absorbed", decorated.get("S.value"), (2, 4))
    check(
        "java: annotation edit is the method",
        [u.symbol for u in units_for_file("S.java", "java", _JAVA_ANNOTATED, [3])],
        ["Svc.run"],
    )

    # Blank lines and lone braces between functions are bookkeeping, not review units.
    blanks = units_for_file("C.java", "java", _JAVA_FIXTURE, [2, 5])
    check("java: blank orphans dropped", blanks, [])
    check("java: closing brace dropped", units_for_file("C.java", "java", _JAVA_FIXTURE, [16]), [])

    # A test framework's suite call is a container, not a function: as a function it swallowed
    # every individual test in the file into one unit.
    spec = {n: (s, e) for n, s, e in _brace_spans(_TS_SPEC)}
    check(
        "ts: one unit per test",
        sorted(spec),
        [
            "OrderService.beforeEach",
            "OrderService.it: places an order",
        ],
    )
    check("ts: describe is not a unit", any("describe" in n for n in spec), False)
    check("ts: test span", spec.get("OrderService.it: places an order"), (8, 10))

    # A closed sibling suite must leave the container stack, or every later unit inherits its
    # name. This is the `Outer.Tags.Sorting.beforeEach` failure.
    nested_names = sorted(n for n, _, _ in _brace_spans(_TS_NESTED))
    check(
        "ts: sibling suites do not accumulate",
        nested_names,
        [
            "Grid.Sorting.it: sorts by name",
            "Grid.Tags.it: adds a tag",
        ],
    )

    # A class whose body opens and closes on one line must not stay on the container stack.
    empty = sorted(n for n, _, _ in _brace_spans(_TS_EMPTY_CLASS))
    check("ts: one-line class does not leak", empty, ["Component.it: works"])

    # A name reached through a receiver is a call, not a declaration.
    module = {n: (s, e) for n, s, e in _brace_spans(_TS_MODULE)}
    check("ts: receiver call is not a function", sorted(module), ["list"])

    # A multi-line lambda inside a method body must stay attributed to the method, and a
    # qualified return type must not be mistaken for a receiver.
    java_lambda = {n: (s, e) for n, s, e in _brace_spans(_JAVA_LAMBDA)}
    check("java: lambda not a unit", sorted(java_lambda), ["Svc.names", "Svc.run"])
    check(
        "java: lambda body is the method",
        [u.symbol for u in units_for_file("S.java", "java", _JAVA_LAMBDA, [4])],
        ["Svc.run"],
    )
    check("java: qualified return type ok", java_lambda.get("Svc.names"), (8, 10))

    # Mechanical edits are recognised without a model — and a changed decision is never mechanical.
    check("plumbing: import", _is_plumbing("import java.util.List;"), True)
    check("plumbing: field", _is_plumbing("    private final Repo repo;"), True)
    check("plumbing: annotation", _is_plumbing("    @Override"), True)
    check("plumbing: ts field", _is_plumbing("  readonly name: string;"), True)
    check("plumbing: kotlin val", _is_plumbing("    val retries: Int = 3"), True)
    check("plumbing: comment", _is_plumbing("    // explains the why"), True)
    check("plumbing: return is not", _is_plumbing("        return items.size();"), False)
    check("plumbing: condition is not", _is_plumbing("        if (id == null) {"), False)
    check("plumbing: call is not", _is_plumbing("    Foo x = build(y);"), False)
    check("plumbing: assignment is not", _is_plumbing("        retries = 5;"), False)
    check(
        "subs: rename",
        _substitutions(["repo.save(id);"], ["repository.save(id);"]),
        [("repo", "repository")],
    )
    check(
        "subs: operator change is not a rename",
        _substitutions(["return x + 1;"], ["return x - 1;"]),
        None,
    )
    check(
        "subs: literal change is not a rename",
        _substitutions(["int max = 5;"], ["int max = 7;"]),
        None,
    )
    check(
        "subs: keyword change is not a rename",
        _substitutions(["return true;"], ["return false;"]),
        None,
    )
    renames = _rename_sites([Hunk(1, ["repo.save(a);"], ["repository.save(a);"])] * 3)
    check("rename needs three sites", ("repo", "repository") in renames, True)
    check(
        "rename: two sites is a retarget",
        _rename_sites([Hunk(1, ["repo.save(a);"], ["repository.save(a);"])] * 2),
        {},
    )
    check(
        "trivial: constant tweak keeps substance",
        _triviality([Hunk(1, ["int max = 5;"], ["int max = 7;"])], {}),
        None,
    )
    check(
        "trivial: new import only",
        _triviality([Hunk(1, [], ["import java.util.List;"])], {}) is not None,
        True,
    )
    check(
        "trivial: rename applied throughout",
        _triviality([Hunk(1, ["repo.save(a);"], ["repository.save(a);"])], renames) is not None,
        True,
    )
    check(
        "trivial: deleted guard keeps substance",
        _triviality([Hunk(9, ["        if (id == null) return;"], [])], {}),
        None,
    )
    check(
        "trivial: plumbing plus logic keeps substance",
        _triviality([Hunk(1, [], ["import x;", "total += fee;"])], {}),
        None,
    )

    # A deletion-only hunk still anchors a unit — a removed guard clause must not vanish.
    check("hunk: deletion anchors a line", Hunk(7, ["x"], []).new_lines, [7])
    check("hunk: addition spans its lines", Hunk(7, [], ["a", "b"]).new_lines, [7, 8])

    # Tags come from the changed lines and the signature, not the whole file.
    priced = Unit("P.java", "java", "Svc.total", 1, 3, [2], 1, 3)
    check(
        "tags: money",
        "money"
        in _tags_for(priced, ["public BigDecimal total() {", "  return price.multiply(qty);", "}"]),
        True,
    )
    check(
        "tags: none on plain code",
        _tags_for(priced, ["public int total() {", "  return items.size();", "}"]),
        [],
    )
    check(
        "tags: lens file",
        Unit("P.java", "java", "f", 1, 1, [1], 1, 1, tags=["authz"]).lenses,
        ["lens_security.md"],
    )

    # Coupled units stay together when the batch cap would otherwise split them.
    a = Unit("C.java", "java", "C.a", 10, 15, [11], 1, 60)
    b = Unit("C.java", "java", "C.b", 30, 35, [31], 1, 60)
    c = Unit("C.java", "java", "C.c", 50, 55, [51], 1, 60)
    a.siblings, c.siblings = [c.id], [a.id]
    check(
        "coupling: caller and callee share a batch",
        [[u.symbol for u in bt.units] for bt in batch_units([a, b, c], max_units=2)],
        [["C.a", "C.c"], ["C.b"]],
    )
    # A high-risk unit narrows its batch to itself plus one helper.
    hot = Unit("C.java", "java", "C.hot", 1, 5, [2], 1, 60, risk="high")
    cold1 = Unit("C.java", "java", "C.cold1", 10, 15, [11], 1, 60)
    cold2 = Unit("C.java", "java", "C.cold2", 20, 25, [21], 1, 60)
    check(
        "risk: high-risk batch holds at most two",
        max(len(bt.units) for bt in batch_units([hot, cold1, cold2])),
        2,
    )
    # A long function reviews alone even when its diff is small.
    long_fn = Unit("C.java", "java", "C.long", 1, 400, [200], 1, 500)
    short_fn = Unit("C.java", "java", "C.short", 410, 415, [411], 1, 500)
    check(
        "body: long function reviews alone",
        [len(bt.units) for bt in batch_units([long_fn, short_fn])],
        [1, 1],
    )
    # Coupling links are recorded on both ends, from the caller's body.
    caller = Unit("S.py", "python", "book", 1, 3, [2], 1, 6)
    callee = Unit("S.py", "python", "priceFor", 5, 6, [6], 1, 6)
    _link_units(
        [caller, callee],
        {
            "S.py": [
                "def book(x):",
                "    return priceFor(x)",
                "",
                "",
                "def priceFor(x):",
                "    return 1",
            ]
        },
    )
    check("links: both ends", (caller.siblings, callee.siblings), ([callee.id], [caller.id]))
    check("ranges", _ranges([3, 4, 5, 9, 12, 13]), "3-5, 9, 12-13")

    # A call site is not a definition, even when the language has no semicolons.
    check("def: python def", _is_definition("def compute_total(items):"), True)
    check("def: java method", _is_definition("    public int total(List<Item> items) {"), True)
    check("def: kotlin fun", _is_definition("    fun total(items: List<Item>): Int {"), True)
    check(
        "def: python return call is a call",
        _is_definition("    return compute_total(items)"),
        False,
    )
    check("def: java call statement is a call", _is_definition("        repo.save(order);"), False)
    check("def: assert call is a call", _is_definition("    assert compute_total([1]) == 1"), False)

    # --- cards: the review unit, sized for coherence and never for cost -----------------
    # A test file is one suite, however many cases it holds. This is where half the original
    # invocations went: a 352-line new test module became 46 units and twelve agents.
    suite = [
        Unit(
            "tests/test_x.py",
            "python",
            f"T.test_{i}",
            i * 5 + 1,
            i * 5 + 4,
            [i * 5 + 2],
            1,
            200,
            is_test=True,
        )
        for i in range(40)
    ]
    suite_cards = make_cards(suite)
    check("cards: a test file is one suite card", len(suite_cards), 1)
    check("cards: suite keeps every case", len(suite_cards[0].units), 40)
    check("cards: suite kind", suite_cards[0].kind, "suite")

    # A mostly-new file is one artifact read whole, not a slice per group of four functions.
    fresh = [
        Unit(
            "app/new.py",
            "python",
            f"f{i}",
            i * 10 + 1,
            i * 10 + 9,
            list(range(i * 10 + 1, i * 10 + 10)),
            9,
            100,
        )
        for i in range(10)
    ]
    fresh_cards = make_cards(fresh)
    check("cards: a new file is one card", len(fresh_cards), 1)
    check("cards: new file kind", fresh_cards[0].kind, "new-file")
    check("cards: new file is read whole", fresh_cards[0].read_lines, 100)

    # A modified file groups by the class its changed methods belong to.
    def owned(symbol: str, owner: str, start: int) -> Unit:
        u = Unit("app/svc.py", "python", symbol, start, start + 10, [start + 1], 1, 900)
        u.owner = owner
        return u

    mixed = [
        owned("A.one", "A", 10),
        owned("A.two", "A", 30),
        owned("B.three", "B", 50),
        owned("helper", "", 70),
    ]
    kinds = [(c.kind, len(c.units)) for c in make_cards(mixed)]
    check(
        "cards: grouped by class, module residue last",
        kinds,
        [("class", 2), ("class", 1), ("region", 1)],
    )

    # A nested function is NOT a class. `_python_spans` qualifies `pack_cards.fits` exactly as it
    # qualifies `Card.title`, so grouping on the first segment made every closure its own card.
    py_src = (
        "class Card:\n"
        "    def title(self):\n"
        "        return 1\n"
        "\n"
        "def pack(xs):\n"
        "    def fits(x):\n"
        "        return True\n"
        "    return fits\n"
    )
    classes = _python_class_prefixes(py_src)
    check("owner: ast reports the class", sorted(classes), ["Card"])
    check("owner: a method is owned by its class", _owner_of("Card.title", classes), "Card")
    check("owner: a closure is owned by nothing", _owner_of("pack.fits", classes), "")
    check("owner: a free function is owned by nothing", _owner_of("pack", classes), "")
    nested = units_for_file("app/p.py", "python", py_src, [3, 7])
    check(
        "cards: a closure joins the module region, not a class of its own",
        sorted((c.kind, len(c.units)) for c in make_cards(nested)),
        [("class", 1), ("region", 1)],
    )
    # A brace language's dotted prefix always comes from `_CONTAINER`, so it is a class already.
    check(
        "owner: brace prefix is a container",
        _owner_of("OrderService.book", set()),
        "OrderService",
    )

    # Splitting is judged on the worst half, because it is about what ONE agent must read.
    wide = [
        Unit("app/wide.py", "python", None, i * 60 + 1, i * 60 + 3, [i * 60 + 2], 1, 3000)
        for i in range(26)
    ]
    wide_cards = make_cards(wide)
    check("cards: a card too wide to read is split", len(wide_cards) > 1, True)
    check("cards: and every unit survives the split", sum(len(c.units) for c in wide_cards), 26)
    check(
        "cards: each split card is within the read ceiling",
        max(c.read_lines for c in wide_cards) <= CARD_MAX_READ,
        True,
    )

    # Reading is windows, not the whole file, once the file is mostly unchanged. This is what
    # made one changed line in an 1,819-line file cost a full-file read.
    tiny = [Unit("app/big.py", "python", "f", 100, 110, [105], 1, 1800)]
    check("read: a small change reads a window, not the file", read_lines_for(tiny), 11 + CTX_LINES)
    check(
        "read: a mostly-new file reads whole",
        read_lines_for([Unit("app/n.py", "python", "f", 1, 50, list(range(1, 51)), 50, 60)]),
        60,
    )
    check(
        "read: the window never exceeds the file",
        read_lines_for([Unit("app/s.py", "python", "f", 1, 8, [4], 1, 12)]),
        12,
    )

    # --- packing: the invocation, sized for cost and never for coherence ----------------
    smalls = [
        _card(f"app/m{i}.py", [Unit(f"app/m{i}.py", "python", "f", 1, 6, [2], 1, 40)], "region")
        for i in range(6)
    ]
    packed = pack_cards(smalls, max_read=600, max_cards=6)
    check("pack: six small cards share one invocation", len(packed), 1)
    check("pack: the invocation keeps all six as separate cards", len(packed[0].cards), 6)
    check("pack: it spans files on purpose", packed[0].spans_files, True)
    check("pack: card count is bounded", len(pack_cards(smalls, max_read=600, max_cards=2)), 3)
    check("pack: reading is bounded", len(pack_cards(smalls, max_read=80, max_cards=6)), 3)

    # Production and test cards never share an invocation: the agent budget reserves test slots
    # by `is_test`, and a mixed invocation would make that reservation meaningless.
    prod_card = _card("app/a.py", [Unit("app/a.py", "python", "f", 1, 4, [2], 1, 50)], "region")
    test_card = _card(
        "tests/test_a.py",
        [Unit("tests/test_a.py", "python", "T.t", 1, 4, [2], 1, 50, is_test=True)],
        "suite",
    )
    check("pack: production and tests stay separate", len(pack_cards([prod_card, test_card])), 2)

    # A card that busts every budget on its own still gets reviewed, alone.
    whale = _card(
        "app/whale.py",
        [Unit("app/whale.py", "python", "f", 1, 2000, list(range(1, 2001)), 2000, 2000)],
        "new-file",
    )
    whale_packed = pack_cards([whale, prod_card], max_read=600)
    check("pack: an oversized card is kept, alone", len(whale_packed), 2)
    check("pack: and it is not silently dropped", sum(len(b.units) for b in whale_packed), 2)

    # `--fit` relaxes the budgets until everything fits, and cannot go below the prod/test split.
    check("fit: everything fits the budget", len(fit_cards(smalls, 2)) <= 2, True)
    check("fit: covers every card", sum(len(b.cards) for b in fit_cards(smalls, 2)), 6)
    check(
        "fit: cannot mix production and tests to hit 1",
        len(fit_cards([prod_card, test_card], 1)),
        2,
    )

    # A suite's many cases are one review target, so they must not be reported as crowding.
    stats_suite = card_stats(pack_cards(suite_cards))
    check("stats: a suite is not oversized", stats_suite["oversized_cards"], 0)
    check(
        "stats: a sliced card with many functions is",
        card_stats(
            pack_cards(
                [
                    _card(
                        "app/s.py",
                        [
                            Unit(
                                "app/s.py",
                                "python",
                                f"A.m{i}",
                                i * 3 + 1,
                                i * 3 + 2,
                                [i * 3 + 1],
                                1,
                                400,
                            )
                            for i in range(10)
                        ],
                        "class",
                    )
                ]
            )
        )["oversized_cards"],
        1,
    )

    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print(f"{'FAILED' if failures else 'OK'}: {len(failures)} failure(s)")
    return 1 if failures else 0


# --- cli ------------------------------------------------------------------------------


def _warnings(stats: dict, skipped: list[Batch], args, cards: dict | None = None) -> list[str]:
    """Things the caller must repeat to the developer, not swallow."""
    out: list[str] = []
    # Counted per card, and only on cards that slice a file: a suite card's many test cases are
    # one review target, so reporting them as crowding would fire on almost every change and
    # train the reader to ignore the warning.
    oversized = (cards or {}).get("oversized_cards", stats["oversized"])
    largest = (cards or {}).get("max_sliced_units", stats["max_units"])
    if oversized:
        out.append(
            f"{oversized} review card(s) hold more than {BATCH_WARN_UNITS} changed functions "
            f"(largest: {largest}). One agent reviewing that many in a single target is the "
            f"attention decay this pass exists to avoid. Prefer a larger budget, or triage a "
            f"subset and review it properly."
        )
    if cards and cards["crowded"]:
        out.append(
            f"{cards['crowded']} invocation(s) hold more than {CARD_WARN_PER_INVOCATION} "
            f"independent review targets (largest: {cards['max_cards']}). The count of distinct "
            f"targets is the better predictor of a reviewer losing depth, and this is a guess "
            f"rather than a measured limit — see the canary benchmark."
        )
    if cards and cards["max_read"] > args.max_read * 2:
        out.append(
            f"one invocation must read {cards['max_read']} lines, more than twice the "
            f"{args.max_read}-line budget. A single cohesive artifact this large is kept whole "
            f"on purpose, but it is the invocation most likely to be reviewed shallowly."
        )
    if skipped:
        out.append(
            f"{len(skipped)} invocation(s) will not be reviewed at all. Report them by name."
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", type=Path, default=Path("."), help="repository (default: cwd)")
    ap.add_argument(
        "--also-repo",
        action="append",
        default=[],
        metavar="PATH",
        help="another repository this change touches: search it for callers and tests too, "
        "so a contract whose only callers live there is not reported as having none "
        "(repeatable; its own diff is reviewed by its own run)",
    )
    ap.add_argument(
        "--base", help="compare from this ref (default: merge base with the trunk branch)"
    )
    ap.add_argument("--head", help="compare to this ref (default: the working tree)")
    ap.add_argument("--staged", action="store_true", help="only staged changes")
    ap.add_argument("--uncommitted", action="store_true", help="only changes not yet committed")
    ap.add_argument("--files", nargs="*", help="restrict to these paths")
    ap.add_argument(
        "--fit",
        type=int,
        metavar="N",
        help="pack EVERY unit into at most N batches by growing them, instead of "
        "keeping batches small and dropping those over --max-batches",
    )
    ap.add_argument(
        "--select",
        nargs="*",
        metavar="ID",
        help="review only these unit ids, in this order (from a triage pass); the "
        "volume ordering is replaced by this order and --max-batches still bounds "
        "the agents, counted in batches",
    )
    ap.add_argument(
        "--fallback",
        nargs="*",
        metavar="ID",
        help="with --select: units to add, in this order, while agents remain under "
        "--max-batches (a triage pass's deprioritised list)",
    )
    ap.add_argument(
        "--high",
        nargs="*",
        metavar="ID",
        help="with --select: units a triage pass judged high-risk; they batch alone "
        "or with one coupled helper",
    )
    ap.add_argument(
        "--max-batches",
        type=int,
        default=MAX_BATCHES,
        help=f"agent budget: batches to review (default {MAX_BATCHES}; 0 = unlimited)",
    )
    ap.add_argument(
        "--max-units",
        type=int,
        default=BATCH_MAX_UNITS,
        help=f"functions per agent batch (default {BATCH_MAX_UNITS}; 1 = one agent per function)",
    )
    ap.add_argument(
        "--max-lines",
        type=int,
        default=BATCH_MAX_LINES,
        help=f"changed lines per agent batch (default {BATCH_MAX_LINES})",
    )
    ap.add_argument(
        "--max-read",
        type=int,
        default=INVOCATION_MAX_READ,
        help=f"lines of code one agent reads across all its cards (default {INVOCATION_MAX_READ})",
    )
    ap.add_argument(
        "--max-cards",
        type=int,
        default=INVOCATION_MAX_CARDS,
        help=f"independent review targets per agent (default {INVOCATION_MAX_CARDS})",
    )
    ap.add_argument(
        "--max-changed",
        type=int,
        default=INVOCATION_MAX_CHANGED,
        help=f"changed lines per agent (default {INVOCATION_MAX_CHANGED})",
    )
    ap.add_argument(
        "--max-symbols",
        type=int,
        default=INVOCATION_MAX_SYMBOLS,
        help=f"changed functions per agent, across all its cards "
        f"(default {INVOCATION_MAX_SYMBOLS})",
    )
    ap.add_argument(
        "--include-trivial",
        action="store_true",
        help="batch mechanical edits (imports, declarations, renames) too, instead of "
        "listing them as not reviewed",
    )
    ap.add_argument(
        "--no-context",
        action="store_true",
        help="skip the git greps for callers, tags and coupling (faster, blinder)",
    )
    ap.add_argument(
        "--briefs-dir",
        metavar="DIR",
        help="write context.txt and one batch-N.txt brief per selected batch here",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--self-test", action="store_true", help="run the built-in fixtures and exit")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if args.staged and args.uncommitted:
        ap.error("--staged and --uncommitted are mutually exclusive")

    def ids_of(parts: list[str] | None) -> list[str]:
        return [i.strip() for part in (parts or []) for i in part.split(",") if i.strip()]

    siblings: list[Sibling] = []
    for raw in args.also_repo:
        path = Path(raw).expanduser().resolve()
        if not (path / ".git").exists():
            ap.error(f"--also-repo {raw}: not a git repository")
        if path == args.repo.resolve():
            ap.error(f"--also-repo {raw}: that is the repo being reviewed")
        siblings.append(Sibling(label=path.name, path=path))
    if len({s.label for s in siblings}) != len(siblings):
        ap.error(
            "--also-repo paths must have distinct directory names, since the name labels the findings"
        )

    scope, label = resolve_scope(args.repo, args.base, args.head, args.staged, args.uncommitted)
    # A committed range must be read at its own head, not from whatever is checked out.
    read_ref = args.head if (args.base and args.head) else None
    all_units = collect(
        args.repo,
        scope,
        args.files,
        context=not args.no_context,
        read_ref=read_ref,
        siblings=siblings,
    )
    trivial = [u for u in all_units if u.trivial]
    # Mechanical edits are listed, not batched: a compiler already checked them, and an agent spent
    # on one is the waste triage exists to prevent. They stay visible so the cut is never silent.
    units = all_units if args.include_trivial else [u for u in all_units if not u.trivial]

    unknown: list[str] = []
    refilled: list[str] = []
    selected_by_id = False
    if args.select:
        # An explicit id may name a trivial unit; the caller asked for it, so it is reviewed.
        units, unknown = select_by_id(all_units, ids_of(args.select))
        for u in units:
            if u.id in set(ids_of(args.high)):
                u.risk = "high"
        selected_by_id = True

    def pack(us: list[Unit], preserve: bool = False) -> list[Batch]:
        """Units -> coherent cards -> agent invocations. The one path the run actually uses."""
        return pack_cards(
            make_cards(us),
            args.max_read,
            args.max_cards,
            args.max_changed,
            args.max_symbols,
            preserve_order=preserve,
        )

    if args.fit:
        # Cover everything within the budget by growing invocations, not by dropping the tail.
        batches = fit_cards(make_cards(units), args.fit, preserve_order=selected_by_id)
        all_batches, skipped = batches, []
    elif selected_by_id:
        # A triage pass ranked these. Its order is kept, and the budget is spent in the currency
        # it is actually paid in — agents, i.e. batches — rather than in units, which under-spent:
        # ten units in three files cost three agents and left seven idle.
        all_batches = pack(units, preserve=True)
        budget = args.max_batches
        if budget and len(all_batches) > budget:
            batches, skipped = all_batches[:budget], all_batches[budget:]
        else:
            batches, skipped = all_batches, []
            if budget and args.fallback:
                chosen = {u.id for u in units}
                by_id = {u.id: u for u in all_units}
                pool = [
                    by_id[i]
                    for i in ids_of(args.fallback)
                    if i in by_id and i not in chosen and not by_id[i].trivial
                ]
                units, batches, refilled = refill(
                    units,
                    pool,
                    budget,
                    args.max_units,
                    args.max_lines,
                    pack=lambda us: pack(us, preserve=True),
                )
                all_batches = batches
    else:
        all_batches = pack(units)
        batches, skipped = select_batches(all_batches, args.max_batches)

    stats = batch_stats(batches)
    cards = card_stats(batches)
    prod = [b for b in batches if not b.is_test]
    tests = [b for b in batches if b.is_test]
    signals = (
        change_signals(args.repo, scope, all_units, siblings)
        if not args.no_context
        else {"architect": [], "integration": [], "maker": []}
    )
    briefs = (
        write_briefs(Path(args.briefs_dir), label, scope, all_units, batches, signals, siblings)
        if args.briefs_dir
        else None
    )

    if args.json:
        print(
            json.dumps(
                {
                    "scope": label,
                    "scope_args": scope,
                    "siblings": [{"label": s.label, "path": str(s.path)} for s in siblings],
                    "unknown_ids": unknown,
                    "units": [as_json(u) for u in units],
                    "trivial": [as_json(u) for u in trivial],
                    "batches": [batch_as_json(b) for b in batches],
                    "skipped": [batch_as_json(b) for b in skipped],
                    "refilled": refilled,
                    "call_links": call_links(all_units),
                    "signals": signals,
                    "lenses": sorted(
                        {lens for u in all_units if not u.trivial for lens in u.lenses}
                    ),
                    "briefs": briefs,
                    "counts": {
                        "units": len(units),
                        "trivial": len(trivial),
                        "high_risk": sum(1 for u in units if u.risk == "high"),
                        "batches_total": len(all_batches),
                        "batches_selected": len(batches),
                        "batches_skipped": len(skipped),
                        "production_batches": len(prod),
                        "test_batches": len(tests),
                        **stats,
                        **cards,
                    },
                    "warnings": _warnings(stats, skipped, args, cards),
                },
                indent=2,
            )
        )
        return 0

    print(f"Reviewing {label}")
    if unknown:
        print(f"  WARNING: {len(unknown)} unrecognised id(s): {', '.join(unknown)}")
    if not units and not trivial:
        print("  nothing function-reviewable in this scope")
        return 0
    print(
        f"{len(units)} changed function(s) in {cards['cards']} review card(s), "
        f"packed into {len(all_batches)} agent invocation(s); running {len(batches)} — "
        f"{len(prod)} production, {len(tests)} test"
        + (f"; {len(trivial)} mechanical edit(s) set aside" if trivial else "")
    )
    print(
        f"{cards['read_lines']} line(s) of code to read in total "
        f"(max {cards['max_read']} in one invocation)"
    )
    for heading, group in (("production", prod), ("tests", tests)):
        if not group:
            continue
        print(f"\n  {heading}:")
        for b in group:
            flags = f"  [{b.risk}]" if b.risk == "high" else ""
            print(
                f"    agent: {len(b.cards) or 1} card(s), {b.read_lines:4d} lines to read, "
                f"{b.changed_count:4d} changed{flags}"
            )
            for c in b.cards or []:
                print(
                    f"      card [{c.kind}] {c.title}  "
                    f"({c.read_lines} to read, {c.changed_count} changed)"
                )
                for u in c.units:
                    extra = []
                    if u.tags:
                        extra.append("touches " + ",".join(u.tags))
                    if u.caller_count:
                        extra.append(f"{u.caller_count} call sites")
                    if u.untested:
                        extra.append("untested")
                    print(
                        f"          · {u.symbol or '(outside any function)'} "
                        f"[{u.start_line}-{u.end_line}]  id={u.id}"
                        + (f"  ({'; '.join(extra)})" if extra else "")
                    )
    if refilled:
        print(f"\n  refilled {len(refilled)} unit(s) from the deprioritised list into idle agents")
    if skipped:
        print(
            f"\n  NOT reviewed — {len(skipped)} batch(es) beyond the agent budget "
            f"of {args.max_batches}:"
        )
        for b in skipped:
            kind = "test" if b.is_test else "production"
            print(f"    {b.changed_count:4d} changed  {b.label}  ({kind})")
    if trivial and not args.include_trivial:
        print(
            f"\n  NOT reviewed — {len(trivial)} mechanical edit(s) (--include-trivial to review them):"
        )
        for u in trivial:
            print(f"    {u.file}  {u.symbol or '(outside any function)'}  — {u.trivial_reason}")
    print(
        f"\n  invocation load: mean {cards['mean_cards']} card(s), max {cards['max_cards']}; "
        f"mean {stats['mean_units']}, max {stats['max_units']} changed function(s)"
    )
    for pass_name, reasons in signals.items():
        print(f"  {pass_name} signals: " + ("; ".join(reasons) if reasons else "none"))
    if briefs:
        print(f"\n  briefs written to {Path(args.briefs_dir)}")
    for w in _warnings(stats, skipped, args, cards):
        print(f"\n  WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
