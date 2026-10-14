#!/usr/bin/env python3
"""Render a review as one self-contained HTML page: the diff, with each finding under its line.

A terminal list of findings makes the reader do the joining — open the file, find the line, hold
the diff in their head — and that is exactly the work a merge-request view does for them. This
produces that view locally, with no MR, no server and no dependencies: one file under `/tmp`,
opened in a browser. It is deliberately a local file rather than a hosted page, because it
contains the repository's diff and quoted code; sharing it is a decision to take each time, not
a default the tool makes.

What the page holds:

- the unified diff, one section per file, with every finding rendered as a card directly under
  the new-side line it anchors to; a finding anchored outside the hunks gets a few lines of the
  working-tree file as context instead of being lost;
- a header with the counts, filters by severity and pass, and the top three findings;
- deep links on every card that open the file at that line in VS Code or IntelliJ;
- a valid / invalid / duplicate label on every card, kept in the browser's local storage per
  review title, with an export button. Every review you read is a precision measurement, and
  those labels are the evidence the benchmark needs;
- the reducer's own drops with their reasons, and the plan's set-aside and skipped work.

Optionally it also emits the same findings as SARIF (for VS Code's SARIF Viewer extension) and as
IntelliJ inspection XML (Code › Analyze Code › View Offline Inspection Results), so the IDE's own
problems view can be used instead.

    render_review.py --repo . --diff /tmp/review.diff --merged /tmp/review-merged.json \\
        --plan /tmp/review-plan.json --title "feature/x" --out /tmp/review-feature-x.html
    render_review.py --repo <clone> --diff mr.diff --merged ~/.cache/mr-reviewer/reviews/<p>-<iid>.json --out mr.html
    render_review.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "SUGGESTION": 2}
STAGE_ORDER = {"PO": 0, "MAKER": 1, "ARCHITECT": 2, "INTEGRATION": 3, "FUNCTION": 4, "SENIOR": 4}
CONTEXT_LINES = 3  # of working-tree context around a finding anchored outside the diff
TOP_FINDINGS = 3

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# --- diff -------------------------------------------------------------------------------


@dataclass
class DiffLine:
    kind: str  # "hunk" | "context" | "add" | "del"
    old: int | None
    new: int | None
    text: str


@dataclass
class DiffFile:
    path: str = ""
    old_path: str | None = None
    status: str = "modified"  # modified | added | deleted | renamed
    binary: bool = False
    lines: list[DiffLine] = field(default_factory=list)


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_unified_diff(text: str) -> list[DiffFile]:
    """Git's unified diff into files, hunks and numbered lines. Tolerant of what it does not know."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    old = new = 0
    in_hunk = False
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current = DiffFile()
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        if not in_hunk:
            if raw.startswith("--- "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    current.status = "added"
                else:
                    current.old_path = _strip_prefix(p)
                continue
            if raw.startswith("+++ "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    current.status = "deleted"
                    current.path = current.old_path or current.path
                else:
                    current.path = _strip_prefix(p)
                    if (
                        current.old_path
                        and current.old_path != current.path
                        and current.status == "modified"
                    ):
                        current.status = "renamed"
                continue
            if raw.startswith("rename from "):
                current.old_path = raw[len("rename from ") :]
                current.status = "renamed"
                continue
            if raw.startswith("rename to "):
                current.path = raw[len("rename to ") :]
                continue
            if raw.startswith(("Binary files", "GIT binary patch")):
                current.binary = True
                continue
        m = _HUNK.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            current.lines.append(DiffLine("hunk", None, None, raw))
            in_hunk = True
            continue
        if not in_hunk or raw.startswith("\\"):
            continue  # metadata before the first hunk, or "\ No newline at end of file"
        if raw.startswith("+"):
            current.lines.append(DiffLine("add", None, new, raw[1:]))
            new += 1
        elif raw.startswith("-"):
            current.lines.append(DiffLine("del", old, None, raw[1:]))
            old += 1
        else:
            current.lines.append(
                DiffLine("context", old, new, raw[1:] if raw.startswith(" ") else raw)
            )
            old += 1
            new += 1
    return [f for f in files if f.path]


# --- findings ---------------------------------------------------------------------------


def load_findings(path: Path) -> tuple[list[dict], list[dict], dict]:
    """(kept, dropped, meta) from the reducer's JSON, the CLI's dry-run cache, or a bare list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, [], {}
    if "kept" in data:
        return (
            data["kept"],
            data.get("dropped", []),
            {"counts": data.get("counts", {}), "posture": data.get("posture")},
        )
    if "comments" in data:
        return data["comments"], [], {"title": data.get("mr_title"), "summary": data.get("summary")}
    raise ValueError(
        f"{path}: not a findings file (expected a list, a reducer result, or a review cache)"
    )


def finding_id(f: dict) -> str:
    """Stable across re-renders of the same finding, so a label survives a re-run that reproduces it."""
    key = f"{f.get('stage')}|{f.get('file')}|{f.get('line')}|{_norm(f.get('title'))}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _norm(title: str | None) -> str:
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())[:50]


def _rank(f: dict) -> tuple:
    return (
        SEVERITY_ORDER.get(f.get("severity", ""), 9),
        STAGE_ORDER.get(f.get("stage", ""), 9),
        f.get("file", ""),
        f.get("line", 0),
    )


# --- markdown (the little the bodies use) -----------------------------------------------

_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _inline(text: str) -> str:
    """Escape first, then the three inline forms the charter bodies use."""
    s = html.escape(text, quote=False)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", s)
    return s.replace("\n", "<br>")


def md_to_html(text: str) -> str:
    """Fenced code, bullet and numbered lists, paragraphs, inline code/bold/italic. Nothing else,
    and everything escaped — the bodies come from agents and are rendered as data."""
    lines = (text or "").splitlines()
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            out.append(f"<p>{_inline(chr(10).join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
            i += 1
            continue
        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                flush()
                items: list[str] = []
                while i < len(lines) and (m := pattern.match(lines[i])):
                    items.append(f"<li>{_inline(m.group(1))}</li>")
                    i += 1
                out.append(f"<{tag}>{''.join(items)}</{tag}>")
                break
        else:
            if not line.strip():
                flush()
            else:
                para.append(line)
            i += 1
    flush()
    return "".join(out)


# --- page -------------------------------------------------------------------------------

_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--add:#dafbe1;--add-fg:#116329;--del:#ffebe9;--del-fg:#82071e;
--hunk:#ddf4ff;--card:#f6f8fa;--crit:#cf222e;--warn:#bf8700;--sugg:#0969da;--redesign:#8250df;--code:#eff1f3;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--line:#30363d;--add:#12261e;--add-fg:#3fb950;
--del:#2d1214;--del-fg:#f85149;--hunk:#0c2d6b;--card:#161b22;--code:#1f242c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--sugg);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px}
header h1{font-size:16px;margin:0 0 4px}header .meta{color:var(--muted);font-size:13px}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-top:8px;font-size:13px}
.controls label{display:inline-flex;gap:4px;align-items:center;cursor:pointer}
.counts span{display:inline-block;padding:1px 8px;border-radius:10px;font-weight:600;font-size:12px;color:#fff;margin-right:4px}
.sev-CRITICAL{background:var(--crit)}.sev-WARNING{background:var(--warn)}.sev-SUGGESTION{background:var(--sugg)}.sev-REDESIGN{background:var(--redesign)}
main{padding:12px 20px 60px;max-width:1400px}
section.top{margin:12px 0 20px}section.top h2{font-size:14px;margin:0 0 6px}
.topcard{border-left:3px solid var(--line);padding:4px 10px;margin:4px 0}
details.file{border:1px solid var(--line);border-radius:6px;margin:12px 0;overflow:hidden}
details.file>summary{padding:8px 12px;background:var(--card);cursor:pointer;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;display:flex;justify-content:space-between;align-items:center}
details.file>summary .n{color:var(--muted);font-family:inherit}
table.diff{width:100%;border-collapse:collapse;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
table.diff td{padding:0 8px;vertical-align:top;white-space:pre-wrap;word-break:break-all}
td.ln{width:1%;min-width:40px;text-align:right;color:var(--muted);user-select:none;border-right:1px solid var(--line)}
tr.add td{background:var(--add)}tr.add td.code{color:var(--add-fg)}tr.del td{background:var(--del)}tr.del td.code{color:var(--del-fg)}
tr.hunk td{background:var(--hunk);color:var(--muted);padding:2px 8px}tr.outside td{background:var(--card);color:var(--muted);font-style:italic;padding:4px 8px}
tr.anchor td.code{outline:2px solid var(--warn);outline-offset:-2px}
tr[id],.card[id]{scroll-margin-top:120px}
tr.cardrow td{padding:6px 12px 10px 60px;background:var(--bg);white-space:normal;word-break:normal}
.card{border:1px solid var(--line);border-left-width:4px;border-radius:6px;background:var(--card);padding:10px 12px;margin:4px 0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.card.CRITICAL{border-left-color:var(--crit)}.card.WARNING{border-left-color:var(--warn)}.card.SUGGESTION{border-left-color:var(--sugg)}.card.design{border-left-color:var(--redesign)}
.card .head{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}.card .head b{font-size:14px}
.badge{font-size:11px;font-weight:600;padding:1px 7px;border-radius:10px;color:#fff}.badge.stage{background:var(--muted)}
.badge.writing{background:transparent;color:var(--muted);font-weight:500;border:1px solid var(--line)}
.card .where{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted);margin-left:auto}
.card .body{margin-top:6px}.card .body p{margin:6px 0}.card .body pre{background:var(--code);padding:8px 10px;border-radius:4px;overflow:auto;font-size:12px}
.card .body code{background:var(--code);padding:0 4px;border-radius:3px;font-size:12.5px}.card .body pre code{background:none;padding:0}
.card .foot{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:8px;font-size:12px;color:var(--muted)}
.labels button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:12px}
.labels button.on{background:var(--fg);color:var(--bg)}.card.labelled-invalid,.card.labelled-duplicate{opacity:.55}
.locs{font-size:12px;color:var(--muted)}
details.aux{margin:16px 0;border:1px dashed var(--line);border-radius:6px;padding:8px 12px}details.aux summary{cursor:pointer;font-weight:600}
details.aux ul{margin:6px 0;padding-left:20px}details.aux li{margin:2px 0}
.hidden{display:none!important}button.export{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:4px;padding:3px 10px;cursor:pointer}
.empty{color:var(--muted);padding:20px}
"""

_JS = """
(function(){
  const META = JSON.parse(document.getElementById('review-meta').textContent);
  const KEY = 'mr-review-labels:' + META.review_id;
  let labels = {};
  try { labels = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { labels = {}; }
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(labels)); } catch (e) {} }
  function paint(card){
    const id = card.dataset.id, label = labels[id];
    card.classList.remove('labelled-valid','labelled-invalid','labelled-duplicate');
    if (label) card.classList.add('labelled-' + label);
    card.querySelectorAll('.labels button').forEach(b => b.classList.toggle('on', b.dataset.label === label));
  }
  document.querySelectorAll('.card').forEach(card => {
    paint(card);
    card.querySelectorAll('.labels button').forEach(b => b.addEventListener('click', () => {
      const id = card.dataset.id;
      if (labels[id] === b.dataset.label) delete labels[id]; else labels[id] = b.dataset.label;
      save(); paint(card); applyFilters();
    }));
  });
  function applyFilters(){
    const sev = new Set([...document.querySelectorAll('input[data-sev]:checked')].map(i => i.dataset.sev));
    const stg = new Set([...document.querySelectorAll('input[data-stage]:checked')].map(i => i.dataset.stage));
    const hideLabelled = document.getElementById('hide-labelled').checked;
    document.querySelectorAll('.card').forEach(card => {
      const show = sev.has(card.dataset.severity) && stg.has(card.dataset.stage) && !(hideLabelled && labels[card.dataset.id]);
      card.classList.toggle('hidden', !show);
      const row = card.closest('tr.cardrow'); if (row) row.classList.toggle('hidden', !show && row.querySelectorAll('.card:not(.hidden)').length === 0);
    });
    document.querySelectorAll('details.file').forEach(d => {
      const visible = d.querySelectorAll('.card:not(.hidden)').length;
      d.querySelector('summary .n').textContent = visible + ' finding' + (visible === 1 ? '' : 's');
    });
  }
  document.querySelectorAll('input[data-sev],input[data-stage],#hide-labelled').forEach(i => i.addEventListener('change', applyFilters));
  document.getElementById('export').addEventListener('click', () => {
    const rows = META.findings.filter(f => labels[f.id]).map(f => Object.assign({}, f, {label: labels[f.id]}));
    const payload = {review: {title: META.title, scope: META.scope, generated: META.generated}, labels: rows};
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'review-labels-' + META.review_id + '.json'; a.click(); URL.revokeObjectURL(a.href);
  });
  applyFilters();
})();
"""


def _badge_label(f: dict) -> str:
    return "REDESIGN" if f.get("kind") == "design" else f.get("severity", "SUGGESTION")


def _editor_links(repo: Path, file: str, line: int) -> str:
    absolute = (repo / file).resolve()
    vs = html.escape(f"vscode://file/{absolute}:{line}")
    idea = html.escape(f"idea://open?file={absolute}&line={line}")
    return f'<a href="{vs}">VS Code</a> · <a href="{idea}">IntelliJ</a>'


def _card(f: dict, repo: Path, anchors: dict[tuple[str, int], str]) -> str:
    fid = finding_id(f)
    sev = f.get("severity", "SUGGESTION")
    kind = f.get("kind", "bug")
    label = _badge_label(f)
    locs = ""
    if f.get("locations"):
        links = []
        for loc in f["locations"][1:]:
            path, _, ln = loc.rpartition(":")
            target = anchors.get((path, int(ln))) if ln.isdigit() else None
            links.append(
                f'<a href="#{target}">{html.escape(loc)}</a>' if target else html.escape(loc)
            )
        locs = f'<div class="locs">Also at: {", ".join(links)}</div>'
    # The writing bar's note, where it acted. A finding whose badge sits a rank below what its
    # pass gave it otherwise reads as an unsure reviewer rather than a wordy one, and the reducer
    # is the only thing that knows which it was.
    bar = ""
    if f.get("writing"):
        note = (
            f"demoted from {f['demoted_from']} · {f['writing']}"
            if f.get("demoted_from")
            else f["writing"]
        )
        bar = f'<span class="badge writing">{html.escape(note)}</span>'
    return (
        f'<div class="card {html.escape(sev)} {html.escape(kind)}" id="f-{fid}" data-id="{fid}" '
        f'data-severity="{html.escape(sev)}" data-stage="{html.escape(f.get("stage", ""))}">'
        f'<div class="head"><span class="badge sev-{html.escape(label)}">{html.escape(label)}</span>'
        f'<span class="badge stage">{html.escape(f.get("stage", ""))}</span>'
        f"<b>{html.escape(f.get('title', ''))}</b>{bar}"
        f'<span class="where">{html.escape(f.get("file", ""))}:{f.get("line", "")}</span></div>'
        f'<div class="body">{md_to_html(f.get("body", ""))}</div>{locs}'
        f'<div class="foot">{_editor_links(repo, f.get("file", ""), int(f.get("line") or 1))}'
        f'<span class="labels">Label: <button data-label="valid">valid</button> '
        f'<button data-label="invalid">invalid</button> <button data-label="duplicate">duplicate</button></span>'
        f"<span>{html.escape(f.get('source', ''))}</span></div></div>"
    )


def _diff_row(line: DiffLine, file_index: int, is_anchor: bool) -> str:
    cls = line.kind + (" anchor" if is_anchor else "")
    if line.kind == "hunk":
        return f'<tr class="hunk"><td class="ln"></td><td class="ln"></td><td class="code">{html.escape(line.text)}</td></tr>'
    old = "" if line.old is None else str(line.old)
    new = "" if line.new is None else str(line.new)
    anchor = f' id="L-{file_index}-{line.new}"' if line.new is not None else ""
    marker = {"add": "+", "del": "-"}.get(line.kind, " ")
    return (
        f'<tr class="{cls}"{anchor}><td class="ln">{old}</td><td class="ln">{new}</td>'
        f'<td class="code">{marker}{html.escape(line.text)}</td></tr>'
    )


def _card_row(cards: list[str]) -> str:
    return f'<tr class="cardrow"><td colspan="3">{"".join(cards)}</td></tr>'


def _outside_context(repo: Path, file: str, line: int) -> list[DiffLine]:
    """A few working-tree lines around a finding the diff does not reach."""
    path = repo / file
    if not path.is_file():
        return []
    source = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(1, line - CONTEXT_LINES), min(len(source), line + CONTEXT_LINES)
    return [DiffLine("context", n, n, source[n - 1]) for n in range(lo, hi + 1)]


def _file_section(
    index: int,
    diff_file: DiffFile | None,
    path: str,
    findings: list[dict],
    repo: Path,
    anchors: dict[tuple[str, int], str],
) -> str:
    by_line: dict[int, list[dict]] = {}
    for f in findings:
        by_line.setdefault(int(f.get("line") or 0), []).append(f)
    rows: list[str] = []
    placed: set[int] = set()
    if diff_file and not diff_file.binary:
        for line in diff_file.lines:
            hit_line = line.new if line.new is not None and line.new in by_line else None
            rows.append(_diff_row(line, index, hit_line is not None))
            if hit_line is not None:
                rows.append(_card_row([_card(f, repo, anchors) for f in by_line[hit_line]]))
                placed.add(hit_line)
    elif diff_file and diff_file.binary:
        rows.append('<tr class="outside"><td colspan="3">binary file</td></tr>')
    for line_no in sorted(n for n in by_line if n not in placed):
        context = _outside_context(repo, path, line_no)
        rows.append(
            '<tr class="outside"><td colspan="3">'
            + (
                "outside the diff — working-tree context"
                if context
                else "outside the diff; file not readable here"
            )
            + "</td></tr>"
        )
        for c in context:
            rows.append(_diff_row(c, index, c.new == line_no))
        rows.append(_card_row([_card(f, repo, anchors) for f in by_line[line_no]]))
    status = (
        f' <span class="n">{diff_file.status}</span>'
        if diff_file and diff_file.status != "modified"
        else ""
    )
    rename = (
        f' <span class="n">← {html.escape(diff_file.old_path)}</span>'
        if diff_file and diff_file.status == "renamed" and diff_file.old_path
        else ""
    )
    n = len(findings)
    open_attr = " open" if n else ""
    return (
        f'<details class="file"{open_attr} id="file-{index}"><summary><span>{html.escape(path)}{status}{rename}</span>'
        f'<span class="n">{n} finding{"" if n == 1 else "s"}</span></summary>'
        f'<table class="diff">{"".join(rows)}</table></details>'
    )


def render_page(
    kept: list[dict],
    dropped: list[dict],
    diff_text: str,
    repo: Path,
    title: str,
    scope: str = "",
    plan: dict | None = None,
    meta: dict | None = None,
) -> str:
    kept = sorted(kept, key=_rank)
    files = parse_unified_diff(diff_text)
    by_path = {f.path: f for f in files}
    order = [f.path for f in files]
    for f in kept:
        if f.get("file") and f["file"] not in by_path:
            order.append(f["file"])
            by_path[f["file"]] = None  # type: ignore[assignment]
    index_of = {path: i for i, path in enumerate(order)}
    anchors: dict[tuple[str, int], str] = {}
    for path, df in by_path.items():
        if df:
            for line in df.lines:
                if line.new is not None:
                    anchors[(path, line.new)] = f"L-{index_of[path]}-{line.new}"

    findings_by_file: dict[str, list[dict]] = {}
    for f in kept:
        findings_by_file.setdefault(f.get("file", ""), []).append(f)

    counts = {s: sum(1 for f in kept if f.get("severity") == s) for s in SEVERITY_ORDER}
    stages = sorted({f.get("stage", "") for f in kept}, key=lambda s: STAGE_ORDER.get(s, 9))
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    review_id = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]

    head = [
        f"<h1>{html.escape(title)}</h1>",
        f'<div class="meta">{html.escape(scope)}{" · " if scope else ""}generated {generated}'
        + (
            f" · posture {html.escape(str(meta.get('posture')))}"
            if meta and meta.get("posture")
            else ""
        )
        + f" · {len(kept)} finding{'' if len(kept) == 1 else 's'} in {len(findings_by_file)} file{'' if len(findings_by_file) == 1 else 's'}</div>",
        '<div class="controls"><span class="counts">'
        + "".join(f'<span class="sev-{s}">{counts[s]} {s.lower()}</span>' for s in SEVERITY_ORDER)
        + "</span>",
        "".join(
            f'<label><input type="checkbox" data-sev="{s}" checked> {s.lower()}</label>'
            for s in SEVERITY_ORDER
        ),
        "".join(
            f'<label><input type="checkbox" data-stage="{html.escape(s)}" checked> {html.escape(s)}</label>'
            for s in stages
        ),
        '<label><input type="checkbox" id="hide-labelled"> hide labelled</label>',
        '<button class="export" id="export">Export labels</button></div>',
    ]

    top = ""
    if kept:
        items = "".join(
            f'<div class="topcard"><span class="badge sev-{_badge_label(f)}">{_badge_label(f)}</span> '
            f'<a href="#f-{finding_id(f)}">{html.escape(f.get("title", ""))}</a> '
            f'<span class="locs">{html.escape(f.get("file", ""))}:{f.get("line", "")} · {html.escape(f.get("stage", ""))}</span></div>'
            for f in kept[:TOP_FINDINGS]
        )
        top = f'<section class="top"><h2>Top findings</h2>{items}</section>'

    sections = [
        _file_section(index_of[p], by_path.get(p), p, findings_by_file.get(p, []), repo, anchors)
        for p in order
    ]
    if not sections:
        sections.append('<div class="empty">Nothing in the diff and no findings.</div>')

    aux: list[str] = []
    if dropped:
        items = "".join(
            f"<li><code>{html.escape(str((d.get('finding') or {}).get('file', '?')))}:{html.escape(str((d.get('finding') or {}).get('line', '?')))}</code> "
            f"{html.escape(str((d.get('finding') or {}).get('title', '(no title)')))} — {html.escape(str(d.get('reason', '')))}</li>"
            for d in dropped
        )
        aux.append(
            f'<details class="aux"><summary>Dropped by the reducer ({len(dropped)})</summary><ul>{items}</ul></details>'
        )
    if plan:
        trivial = plan.get("trivial") or []
        if trivial:
            items = "".join(
                f"<li><code>{html.escape(u.get('file', ''))}</code> {html.escape(u.get('symbol') or '(outside any function)')} — {html.escape(u.get('trivial_reason', ''))}</li>"
                for u in trivial
            )
            aux.append(
                f'<details class="aux"><summary>Mechanical edits set aside ({len(trivial)})</summary><ul>{items}</ul></details>'
            )
        skipped = plan.get("skipped") or []
        if skipped:
            items = "".join(
                f"<li><code>{html.escape(b.get('label', ''))}</code> — {len(b.get('units', []))} function(s), {'test' if b.get('is_test') else 'production'}</li>"
                for b in skipped
            )
            aux.append(
                f'<details class="aux"><summary>Not reviewed — beyond the agent budget ({len(skipped)})</summary><ul>{items}</ul></details>'
            )
        warnings = plan.get("warnings") or []
        if warnings:
            items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
            aux.append(
                f'<details class="aux"><summary>Warnings from the plan ({len(warnings)})</summary><ul>{items}</ul></details>'
            )

    meta_json = json.dumps(
        {
            "review_id": review_id,
            "title": title,
            "scope": scope,
            "generated": generated,
            "findings": [
                {
                    "id": finding_id(f),
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "stage": f.get("stage"),
                    "severity": f.get("severity"),
                    "kind": f.get("kind"),
                    "title": f.get("title"),
                }
                for f in kept
            ],
        }
    ).replace("<", "\\u003c")  # inert inside the JSON script block, and never readable as a tag

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{html.escape(title)}</title><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{_CSS}</style></head><body><header>{''.join(head)}</header><main>{top}{''.join(sections)}{''.join(aux)}</main>"
        f'<script id="review-meta" type="application/json">{meta_json}</script><script>{_JS}</script></body></html>'
    )


# --- IDE imports ------------------------------------------------------------------------

_SARIF_LEVEL = {"CRITICAL": "error", "WARNING": "warning", "SUGGESTION": "note"}
_IDEA_SEVERITY = {"CRITICAL": "ERROR", "WARNING": "WARNING", "SUGGESTION": "WEAK WARNING"}


def to_sarif(kept: list[dict]) -> dict:
    """SARIF 2.1.0, the shape VS Code's SARIF Viewer and GitLab's code-quality import both read."""
    stages = sorted({f.get("stage", "REVIEW") for f in kept})
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {"name": "mr-review", "rules": [{"id": s, "name": s} for s in stages]}
                },
                "results": [
                    {
                        "ruleId": f.get("stage", "REVIEW"),
                        "level": _SARIF_LEVEL.get(f.get("severity", ""), "note"),
                        "message": {"text": f"{f.get('title', '')}\n\n{f.get('body', '')}"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": f.get("file", ""),
                                        "uriBaseId": "%SRCROOT%",
                                    },
                                    "region": {"startLine": int(f.get("line") or 1)},
                                }
                            }
                        ],
                        "partialFingerprints": {"mrReview/v1": finding_id(f)},
                    }
                    for f in kept
                ],
            }
        ],
    }


def to_intellij_xml(kept: list[dict]) -> str:
    """IntelliJ's offline inspection format: Code › Analyze Code › View Offline Inspection Results."""
    problems = []
    for f in kept:
        problems.append(
            "  <problem>\n"
            f"    <file>file://$PROJECT_DIR$/{xml_escape(f.get('file', ''))}</file>\n"
            f"    <line>{int(f.get('line') or 1)}</line>\n"
            f'    <problem_class severity="{_IDEA_SEVERITY.get(f.get("severity", ""), "WEAK WARNING")}" '
            f'attribute_key="{_IDEA_SEVERITY.get(f.get("severity", ""), "WEAK WARNING").replace(" ", "_")}_ATTRIBUTES">'
            f"{xml_escape(f.get('stage', ''))}: {xml_escape(f.get('title', ''))}</problem_class>\n"
            f"    <description>{xml_escape(f.get('body', ''))}</description>\n"
            "  </problem>"
        )
    return "<problems>\n" + "\n".join(problems) + "\n</problems>\n"


# --- self-test --------------------------------------------------------------------------

_DIFF = """\
diff --git a/Svc.java b/Svc.java
index 1111111..2222222 100644
--- a/Svc.java
+++ b/Svc.java
@@ -2,4 +2,5 @@ public class Svc {
     public int total(List<Item> items) {
         int sum = 0;
-        for (Item i : items) sum += i.cost();
+        for (Item i : items) sum += i.price();
+        log.info("sum " + sum);
         return sum;
diff --git a/New.java b/New.java
new file mode 100644
--- /dev/null
+++ b/New.java
@@ -0,0 +1,2 @@
+class New {
+}
"""


def _self_test() -> int:
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    files = parse_unified_diff(_DIFF)
    check("diff: two files", [f.path for f in files], ["Svc.java", "New.java"])
    check("diff: new file status", files[1].status, "added")
    svc = files[0]
    adds = [(dl.new, dl.text) for dl in svc.lines if dl.kind == "add"]
    check(
        "diff: added lines numbered on the new side",
        adds,
        [
            (4, "        for (Item i : items) sum += i.price();"),
            (5, '        log.info("sum " + sum);'),
        ],
    )
    dels = [(dl.old, dl.text) for dl in svc.lines if dl.kind == "del"]
    check(
        "diff: deleted line numbered on the old side",
        dels,
        [(4, "        for (Item i : items) sum += i.cost();")],
    )
    check(
        "diff: context keeps both numbers",
        [(dl.old, dl.new) for dl in svc.lines if dl.kind == "context"][:2],
        [(2, 2), (3, 3)],
    )

    check(
        "md: fence escaped",
        md_to_html("```\n<b>x</b>\n```"),
        "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>",
    )
    check(
        "md: inline code and bold",
        md_to_html("Use `foo()` **now**."),
        "<p>Use <code>foo()</code> <strong>now</strong>.</p>",
    )
    check("md: bullets", md_to_html("- a\n- b"), "<ul><li>a</li><li>b</li></ul>")
    check("md: script tag is data", "<script>" in md_to_html("<script>alert(1)</script>"), False)

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "Svc.java").write_text(
            "public class Svc {\n    public int total(List<Item> items) {\n        int sum = 0;\n"
            '        for (Item i : items) sum += i.price();\n        log.info("sum " + sum);\n'
            "        return sum;\n    }\n    void other() {}\n}\n"
        )
        on_diff = {
            "stage": "FUNCTION",
            "kind": "craft",
            "file": "Svc.java",
            "line": 4,
            "severity": "WARNING",
            "title": "Accumulator loop",
            "body": "```java\nsum += i.price();\n```\nA stream states it.",
        }
        outside = {
            "stage": "ARCHITECT",
            "kind": "bug",
            "file": "Svc.java",
            "line": 8,
            "severity": "SUGGESTION",
            "title": "Class grows a second job",
            "body": "`other()` is unrelated.",
        }
        page = render_page(
            [on_diff, outside],
            [{"finding": {"file": "Svc.java", "line": 9, "title": "x"}, "reason": "unquoted"}],
            _DIFF,
            repo,
            "feature/x",
            scope="branch work",
        )
        check(
            "page: anchor row precedes its card",
            page.index('id="L-0-4"') < page.index(f'id="f-{finding_id(on_diff)}"'),
            True,
        )
        check(
            "page: outside-diff card gets working-tree context",
            "outside the diff — working-tree context" in page,
            True,
        )
        check("page: outside context shows the line", "void other() {}" in page, True)
        check("page: editor links", "vscode://file/" in page and "idea://open?file=" in page, True)
        check("page: dropped section", "Dropped by the reducer (1)" in page, True)
        check(
            "page: file without findings is collapsed",
            '<details class="file" id="file-1">' in page,
            True,
        )
        check(
            "page: file with findings is open",
            '<details class="file" open id="file-0">' in page,
            True,
        )
        check("page: top findings", "Top findings" in page, True)
        check(
            "page: severity ranks first",
            page.index("Accumulator loop") < page.index("Class grows a second job"),
            True,
        )
        sarif = to_sarif([on_diff])
        check("sarif: level", sarif["runs"][0]["results"][0]["level"], "warning")
        check(
            "sarif: line",
            sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"][
                "startLine"
            ],
            4,
        )
        import xml.etree.ElementTree as ET

        tree = ET.fromstring(to_intellij_xml([on_diff, outside]))
        check("idea: two problems", len(tree.findall("problem")), 2)
        problem_class = tree.find("problem/problem_class")
        check(
            "idea: severity",
            problem_class.get("severity") if problem_class is not None else None,
            "WARNING",
        )

    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print(f"{'FAILED' if failures else 'OK'}: {len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--repo", type=Path, default=Path("."), help="repository the findings cite (default: cwd)"
    )
    ap.add_argument("--diff", type=Path, help="the reviewed unified diff")
    ap.add_argument(
        "--merged", type=Path, help="the reducer's JSON, a review cache, or a bare findings list"
    )
    ap.add_argument(
        "--plan", type=Path, help="the mapper's plan JSON, for set-aside and skipped work"
    )
    ap.add_argument("--title", default="Review", help="page title; labels persist per title")
    ap.add_argument("--scope", default="", help="the scope label to show under the title")
    ap.add_argument("--out", type=Path, help="where to write the page (default: stdout)")
    ap.add_argument("--sarif", type=Path, help="also write the findings as SARIF here")
    ap.add_argument(
        "--intellij", type=Path, help="also write the findings as IntelliJ inspection XML here"
    )
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.merged:
        ap.error("--merged is required")

    kept, dropped, meta = load_findings(args.merged)
    diff_text = (
        args.diff.read_text(encoding="utf-8", errors="replace")
        if args.diff and args.diff.is_file()
        else ""
    )
    plan = (
        json.loads(args.plan.read_text(encoding="utf-8"))
        if args.plan and args.plan.is_file()
        else None
    )
    scope = args.scope or (plan or {}).get("scope", "") or ""
    title = args.title if args.title != "Review" else (meta.get("title") or "Review")

    page = render_page(
        kept, dropped, diff_text, args.repo, title, scope=scope, plan=plan, meta=meta
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
        print(args.out)
    else:
        print(page)
    if args.sarif:
        args.sarif.write_text(json.dumps(to_sarif(kept), indent=2), encoding="utf-8")
    if args.intellij:
        args.intellij.write_text(to_intellij_xml(kept), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
