# MR Reviewer

Automated GitLab MR code review tool powered by Claude. Scans open MRs across the configured GitLab projects and posts AI-generated review comments.

## Project Structure

- `mr_reviewer/main.py` — CLI entry point, `review-all` and `review` commands, diff building, parallel MR processing
- `mr_reviewer/reviewer.py` — Claude integration: `LocalReviewer` (clone mode, file access — the supported reviewer); `CLIReviewer` (pipe mode) and `APIReviewer` (direct API) are **deprecated** diff-mode backends
- `mr_reviewer/gitlab_client.py` — GitLab API client for fetching MRs, diffs, posting comments
- `mr_reviewer/orchestrator.py` — Multi-pass review team for clone mode: PO, blind design, Architect, batched Senior, Prompt. It no longer reduces: the merge is `plugins/mr-review/scripts/merge_findings.py`, reached through `mr_reviewer/reduce.py`
- `mr_reviewer/reduce.py` — The package's bridge to that one reduce step: resolves the script the way `charter_dir()` resolves the charter, converts `ReviewComment` objects to candidates and back, and picks the posture from who reads the result
- `mr_reviewer/prompts.py` — Review prompt templates (Java, Angular, generic); prompt bodies live in
  `plugins/mr-review/charter/*.md`, resolved by `charter_dir()` — **not** inside the package.
  `get_base_prompt()` splices `stage_po.md`, `stage_architect.md` and `stage_senior.md` into
  `base.md` at its `<!-- STAGES -->` marker, so the CLI prompt is one assembled rubric
- `plugins/mr-review/` — The `mr-review` Claude Code plugin. Owns the charter, and ships the
  `/review-changes` skill so a developer can review their own work before the MR exists
- `plugins/mr-review/skills/review-changes/SKILL.md` — The skill: resolves the local scope, has the
  mapper split it into per-function units and compute the change context, runs each pass as an
  isolated subagent on a per-pass model, reduces with a script, reports. Read-only — it posts
  nowhere and edits nothing
- `plugins/mr-review/scripts/changed_functions.py` — Maps changed diff lines to their enclosing
  function (`ast` for Python, a signature-and-brace scan elsewhere), including deletion-only hunks;
  computes the change context (tags, callers, coupling, triviality, per-pass signals); batches by
  risk and body length; writes `context.txt` and per-batch briefs. Has `--self-test`
- `plugins/mr-review/scripts/merge_findings.py` — The deterministic reducer: lenient parse, drop
  malformed and unquoted findings, dedup, pattern collapse, calm caps, ranking, markdown render
  with a clickable `path:line` and a code excerpt per finding. Never judges whether a finding is
  right. Has `--self-test`
- `plugins/mr-review/scripts/render_review.py` — Renders the diff with each finding under its
  line as one self-contained local HTML page (filters, editor deep links, valid/invalid/duplicate
  labels with export), plus optional SARIF and IntelliJ inspection XML. Reads the reducer's JSON,
  the CLI's dry-run cache, or a bare list. Has `--self-test`
- `plugins/mr-review/charter/craft_lens.md` — The function-level half of `clean_code.md`, read by
  the craft agent instead of the full charter. `integration.md` — the cross-file behaviour pass.
  `lens_*.md` — short risk lenses (persistence, concurrency, security, money, retry/cache) appended
  to prompts whose units carry the matching tag. `writing.md` — how every finding's title and
  body are written (problem, why it matters, suggested change; short sentences, no essay
  register); read by every finding-producing pass in the skill and appended to the CLI prompt by
  `get_base_prompt()`, because the passes otherwise answer in the charter's own prose voice
- `tests/test_posture.py` — Regression guards for the posture tiers, the runaway guard and charter
  resolution; `tests/test_effort.py` — per-pass effort, including a call-site walk;
  `tests/test_changed_functions.py` and `tests/test_merge_findings.py` — the two scripts against
  real git repositories and real files; `tests/test_charter_references.py` — every charter file
  either consumer names exists, and the stage splice reassembles in order
- `mr_reviewer/config.py` — Configuration loading from `.env`, `config.yaml`, and env vars
- `mr_reviewer/telemetry.py` — The run-record note posted on every run (including `--dry-run`),
  its machine-readable trailer, and the `is_posted_review` gate `has_bot_reviewed` depends on
- `mr_reviewer/repo_context.py` — Git clone/update for local reviewer mode
- `mr_reviewer/related_files.py` — Fetches related files (entities, interfaces) for review context

## Running

```shell
# Install
cd mr-reviewer
poetry install

# Review all open MRs (CLI mode, default)
.venv/bin/mr-reviewer review-all

# Review a single MR (flags only — positional args are NOT supported)
.venv/bin/mr-reviewer review -p <group>/<repo> -m <mr-iid>
.venv/bin/mr-reviewer review --url "https://gitlab.com/<path>/-/merge_requests/<iid>"

# Review locally — prints findings instead of posting them inline (and posts one
# run-record note per MR only if MR_REVIEWER_RECORD_RUNS=1; see "Run records" below)
.venv/bin/mr-reviewer review-all --dry-run

# Learning mode — every comment teaches its concept, plus a study document
.venv/bin/mr-reviewer review -p <group>/<repo> -m <mr-iid> --learn --enable-maker --dry-run

# With the opt-in blind design pass (two extra serial calls)
.venv/bin/mr-reviewer review -p <group>/<repo> -m <mr-iid> --enable-maker --dry-run
MR_REVIEWER_MAKER=1 .venv/bin/mr-reviewer review-all --dry-run
```

### Inspect, then post

A `--dry-run` of `review` caches the finished review (comments + summary) to
`~/.cache/mr-reviewer/reviews/<project>-<iid>.json`. Post that exact review with
`--post-cached` — no clone, no LLM calls, a few seconds:

```shell
mr-reviewer review --url <mr-url> --dry-run     # inspect
mr-reviewer review --url <mr-url> --post-cached # post what you inspected
```

Prefer this over re-running without `--dry-run`. The review is **not
deterministic**: a second run produces a different set of comments at different
lines, so re-running posts something other than what you approved.
`--post-cached` refuses if new commits have landed since the review was taken,
because comment positions are computed against the MR's diff refs. Clone mode
only (`review`, not `review-all`).

## Configuration

- `.env` file in the project root provides `GITLAB_TOKEN` and optionally `ANTHROPIC_API_KEY`
- If `ANTHROPIC_API_KEY` is not set, the tool uses **Claude Code CLI** (`claude -p`) which requires an active Claude login
- Config can also come from `config.yaml` or env vars (see `config.py` for priority order)
- **Nothing organisation-specific is hardcoded.** GitLab host and group, Jira site, Jira project
  keys and the Jira MCP tool name are all settings (`config.example.yaml` lists each with its env
  var). The Jira tool name and project keys are read deep in the prompt builders, so `load_config`
  publishes them to `JIRA_MCP_TOOL` / `JIRA_PROJECT_KEYS` with `setdefault`, the same channel
  `--enable-maker` and `--brutal` use; read them through `config.jira_mcp_tool()` and
  `config.jira_project_keys()`, never by literal

## Scheduling

Reviews are run **on demand** via the CLI commands above. There is **no scheduled runner**. Do not add cron-specific assumptions to the code.

## Review Modes

**Clone mode is the standard, supported review path** and the default for `review` / `review-all`. It clones the repo at the MR branch and runs Claude with file-access tools (`Read,Glob,Grep`) so it can browse the full codebase — which is required for structural and function-level (craft) review quality.

### The blind design pass (independent design) — opt-in

**Off by default.** Enable per run with `--enable-maker` on `review` / `review-all`, or
`MR_REVIEWER_MAKER=1` for scripted runs. It is the longest chain in the team (two serial Claude
calls, one of them generative, roughly ten minutes on a large MR) and is still being calibrated, so
it stays opt-in until its findings have earned a place in the default review.

Most review passes inspect the code that exists and ask whether it is correct, well-shaped, and
readable. That finds local defects but structurally cannot find a **wrong root decision**, because a
wrong root decision does not look like a smell — it looks like clean, well-named code implementing
the wrong thing, and it is only visible against an alternative.

This pass supplies that alternative. It designs the change itself from the story, then diffs its
design against what was built, in two calls (`run_maker` in `orchestrator.py`):

1. **Design** (`charter/blind_design.md`, label `BLIND DESIGN/design`) — runs with its cwd in a
   `git archive` extract of the pre-change commit (`repo_context.create_base_tree`), so the
   implementation is physically absent rather than merely off-limits. An archive rather than a
   worktree, deliberately: a worktree carries the object database, and a design agent that thinks
   to run `git log` reaches the branch it is supposed to be blind to. It receives the **story and
   only the story** — the MR title, description and diff are all withheld, because each narrates
   the chosen implementation — and produces a short sketch: which tier does the work, what types
   cross each boundary, and a few lines proving the alternative is real. It deliberately does *not*
   write the implementation; a parallel implementation would make step 2 diff hundreds of lines and
   report style differences.
2. **Compare** (`charter/blind_compare.md`, label `BLIND DESIGN/compare`) — the only pass that
   knows what the alternative was. Reports a divergence only when its cost can be named concretely,
   and posts **one comment per root decision** listing the symptoms it subsumes.

There is no longer a triage step. An earlier version had one (`maker_triage.md`) naming the
mechanisms worth designing, which made the pass affordable but described the *implementation's own
decomposition* in requirement language — so the design step could ask whether each named mechanism
should exist while never asking whether the change was carved up the right way. Scoping now comes
from the forks the acceptance criteria leave open, which is why the pass needs a Jira story and
skips itself without one.

Both calls read `charter/design_principles.md` — the charter a design is judged against,
organised by SRP, abstractions and wrong abstractions, domain modelling, OOP/FP, design patterns and
their absence, imperative-versus-declarative, and unnecessary complexity and verbosity. It is grounded
in the canonical literature (Parnas, Martin, Fowler, Evans, Beck, Metz, Ousterhout, Meyer) with the
house bar from the dev-flow plugin's `coding-principles.md` folded in, and it is kept in this repo so
the reviewer never depends on a path outside it. `clean_code.md` states the same ideas as things to
*look for* while reading; this file states them as things to *decide* while designing.

Its findings are emitted as stage `MAKER` with `kind == "design"`, which is what the reducer, the
caps and the renderer key off; the stage name is kept because changing it would touch every ranking
site and the posted corpus. They post as `🔷 **REDESIGN**` (or `REDESIGN — MUST FIX` when CRITICAL)
rather than a severity word, and are counted separately in the summary line. That is a rendering
rule keyed off `kind == "design"`, not a fourth `severity` value — severity stays an urgency axis so
the ranking and cap logic keeps working, and a design finding can never outrank a data-loss bug.
Preservation is handled structurally instead: MAKER is exempt from the same-locus collapse (a root
decision and a bug can both be true of one line), and `DESIGN_SLOTS = 2` in the reducer holds two
design findings back from the global ceiling. Craft's protection is different in kind — the per-file
cap (`craft_per_file`) bounds craft rather than reserving slots for it — because the parse-time
`CRAFT_FLOOR` that used to reserve them went with the per-stage caps.

Cost control matters because both calls are large: a free structural gate over the added diff lines
runs first (`maker_candidates`), and on a typical MR it selects a quarter of the changed files or
nothing at all. Test sources are excluded from the gate and migration-shaped paths always pass it.
Findings are labelled "Alternative implementation" to the MR author, and the reduce step folds
matching craft symptoms into the design comment while leaving correctness bugs standalone.

Effort is per call: `BLIND DESIGN/design` at `medium` (generative planning, not review) and
`BLIND DESIGN/compare` at `high` (it holds two designs side by side and prices the divergence).
Those keys must match the labels `run_maker` passes, because the lookup is a `.get` — a key that
matches nothing gives no level at all rather than a wrong one, and the pass silently inherits the
session's. `tests/test_effort.py` reads the call sites out of the source and asserts both
directions, which is the regression that made this worth a test.

> ⚠️ **Deprecated: diff mode.** The older "diff / standard" path — selected by `--no-local-clone`, and the `--use-api` (Anthropic SDK) backend — embeds a *truncated* diff plus separately-fetched file context directly in the prompt instead of cloning. It is **deprecated**: it truncates large MRs (`max_diff_lines`, default 3000) and silently drops trailing files, cannot browse the codebase, and produces materially weaker structural/craft findings. Retained only as a fallback; **do not use it for normal reviews.** It is slated for removal, and new work (e.g. the Agent SDK migration) targets clone mode only.

## Learning mode

`--learn` (or `MR_REVIEWER_LEARN=1`) runs the normal review with every volume cap lifted, then one
extra `claude -p` call (`charter/learning.md` + the design charter) turns the merged findings into a
study document grouped by concept.

It does two things, in this order: `teach_in_comments` appends a concept addendum to each finding
that has one, then `generate_learning_note` writes the concept-grouped study document
(`charter/learning.md`). Both live in `orchestrator.py`; `main.py` runs them in its learning branch.

Design points to preserve when changing this:

- **Both are post-processing, not extra review passes.** The findings already exist; re-deriving them
  would double the cost, and a prompt asked to both hunt and teach does neither well.
- **The addendum is appended, never a rewrite.** An append cannot damage a finding the review already
  got right, which is why enrichment can safely run on merged output instead of being threaded into
  all five review prompts, where it would compete with the finding work for attention.
- **The comment-teaching call gets no diff and no repo access.** Deliberate: the addendum must not
  introduce claims the finding did not establish, and withholding the sources of new claims is the
  cheapest way to enforce that.
- **Two consumers, two volumes.** The document gets every finding, because a concept's second and
  third instance are the lesson. The MR thread gets the run's *normal* posture, applied at the
  posting step in `main.py` — learning mode changes what a comment says, not how many are posted.
  Note that no cap drops CRITICAL/WARNING, so a cap raised here would bound almost nothing; that is
  why the split is by consumer rather than by a bigger number.
- **Concept names must agree** between the addenda and the document. `teach_in_comments` returns the
  names it used and they are passed into the document prompt for exactly this reason.

The relaxation lives in `orchestrate_review`, which reduces at `posture="brutal"` with
`preserve_all=True`: every drop rule and dedup still run, but the caps and symptom absorption do
not. Absorption is the one worth understanding — the symptoms it folds away are the recognisable
surface a developer learns to spot, so a lesson needs them and a review does not. `main.py` then
re-reduces at the run's own posture before posting, which is where the two volumes part.

## Run records

With `MR_REVIEWER_RECORD_RUNS=1`, every run posts exactly one note on the MR, `--dry-run` included,
built by `mr_reviewer/telemetry.py`. A dry run posts *only* that note; a normal review folds the
same counts into its usual summary regardless of the variable. Wired in at the three points where `comments` becomes final: the diff-mode
path, `_review_with_local_clone` (which serves both `review` and `review-all` in clone mode), and
the `review-all` diff-mode worker — including their zero-finding branches, because "reviewed, found
nothing" is worth recording.

Why it exists: a dry run previously left no trace anywhere, so the tool's usage could only ever be
measured from *posted* reviews. Nothing retrospective recovers those runs — GitLab does not audit
repository reads, and a dry run produces no note, no event and no server-side trace.

Design points to preserve when changing this:

- **Opt-in, gated in one place.** `record_local_run` returns early unless
  `MR_REVIEWER_RECORD_RUNS` is set, so every call site inherits the gate. A dry run that writes to
  a shared MR thread is a surprise to anyone who did not ask for it; a team that wants usage
  measured sets the variable for everyone, and an opt-in shared that way is the honest version of
  the always-on policy this replaced.
- **`has_bot_reviewed` must key off the trailer, not the heading.** It reads `mode` via
  `telemetry.is_posted_review`; a local run must not make `review-all` skip the MR forever. A marker
  note with **no** trailer predates this feature and still counts as posted — that branch is what
  keeps older review history readable, and dropping it would silently re-review everything.
- **The heading still starts with `## 🤖 Automated Code Review`.** Every downstream consumer matches
  it with `startswith`, so a mode suffix may follow but nothing may precede it.
- **Repeat runs append; the note carries `run=N`.** History is kept deliberately, so editing one note
  in place is the wrong fix for thread noise.
- **The trailer records `stage` and `kind`,** which `to_markdown()` discards. That discard is why
  pass-level attribution is unrecoverable for the existing corpus; this closes it going forward.
- **Only real fields go in.** No `nits` (the reviewer has no nit severity — `suggestion_craft` is the
  nearest honest measure) and no `implemented` (an outcome is only knowable later, from replies and
  subsequent commits).
- **Recording never fails a review.** `record_local_run` catches everything and warns, because the
  review already happened — but it warns loudly rather than silently, since a team that turned
  recording on is relying on it and a broken recorder means runs go uncounted without anyone noticing.

## The mr-review plugin and the `/review-changes` skill

`plugins/mr-review/` is a Claude Code plugin, published by the marketplace manifest at
`.claude-plugin/marketplace.json` in this repo's root. It covers the half of the problem the CLI
does not: reviewing a change **before the MR exists**, from the developer's own session, with
nothing installed. It is also the home of the per-function craft pass the CLI could never afford.

**The division of labour is the design.** The CLI reviews merge requests — unattended, in batch,
posting to the thread. The skill reviews the local branch — interactive, read-only, posting
nowhere. Do not add MR fetching or comment posting to the skill: by the time an MR exists the
feedback is expensive to act on, and the skill runs against a working tree that may hold
uncommitted work, so writing anything there risks destroying work the user cannot recover.

**Each pass answers one question, and the questions do not overlap.** Craft: is the implementation
locally good? Architect: is the structure good? Integration: does the change behave correctly as
a system? MAKER: was this the right solution? PO: does it do what the story asked? When a finding
could belong to two passes, the charter files say which owns it, and the answer is never "both".

Design points to preserve when changing this:

- **The plugin owns the charter; the package borrows it.** `mr_reviewer/prompts.py::charter_dir()`
  resolves `plugins/mr-review/charter/`, and there is exactly **one** copy of every prompt body.
  This is load-bearing. A plugin install copies only the plugin subtree into
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, so a skill cannot read anything
  above its own root — the charter therefore has to live *inside* the plugin, and the alternative
  (a synced copy plus a drift check) is the failure this repo has already had with its README.
- **`base.md` is spliced, not duplicated.** Its three stage bodies live in `stage_po.md`,
  `stage_architect.md` and `stage_senior.md`, and `get_base_prompt()` splices them back in at the
  `<!-- STAGES -->` marker, so the CLI's assembled prompt is byte-identical to the pre-split file
  (`tests/test_charter_references.py` guards the order). The split exists so the skill's ARCHITECT
  and PO agents can read only their own stage; without it each read the whole rubric, including
  the two stages they were told to ignore. The same reasoning produced `craft_lens.md`: the
  function-level half of `clean_code.md`, which the craft agent reads *instead of* `clean_code.md`
  — the full charter opens with object modelling and module boundaries, which is exactly what that
  agent is told is not its job, and it was roughly half of every craft agent's input tokens.
- **The plugin is located once, and the order of the chain matters.** `prompts.plugin_subdir()`
  is the one implementation: the directory's own override (`$MR_REVIEWER_CHARTER`,
  `$MR_REVIEWER_SCRIPTS`), then that directory inside `$MR_REVIEWER_PLUGIN`, then the plugin
  beside the package, then the pre-move location. The last is not vestigial: it means an older
  checkout or an installed wheel degrades to working rather than raising at import. It raises only
  when every candidate misses, and names them all. `charter_dir()` and `reduce.scripts_dir()` were
  eighteen near-identical lines apiece and had already drifted — one cached, one not — and
  `$MR_REVIEWER_PLUGIN` exists because the charter and the reducer are a matched pair: moving one
  to an older checkout while the other stays put pairs a charter with a reducer that never shipped
  with it, and nothing notices.
- **Do not edit the charter to change one review.** Both consumers read it, so an edit changes what
  the bot posts on every MR. Per-run posture does **not** belong in a prompt at all — it is a
  reducer setting (`POSTURE_CAPS`), because a prompt that asks for less cannot be un-asked
  afterwards. Per-*risk* guidance belongs in a lens file (`lens_*.md`), appended only to the
  prompts whose units carry that tag.
- **The review unit is decided by a script, not the model.**
  `plugins/mr-review/scripts/changed_functions.py` maps every changed line to its enclosing
  function — exactly via `ast` for Python, and via a signature regex plus a brace-depth scan that
  strips strings and comments first for the brace languages — then groups those units into cards
  and packs the cards into invocations. A hunk header gives a *nearby* symbol
  rather than the enclosing one, which is why asking the model to work it out produced units that
  drifted to the wrong function or collapsed into "this file".
- **A committed range is read at its own head, not from the checkout.** `collect(read_ref=...)`
  and `_file_text` take function *bodies* from `git show <head>:<path>`, and `_callers` greps at
  that ref. Reading the working tree instead made the unit list silently wrong whenever the
  checkout did not match the range: the same `--base X --head Y` on a large reference MR gave 61
  units against an unrelated branch and 207 against the range's own tree, warning neither time.
  Review an MR from a worktree, and let the mapper read the ref.
- **The script also computes the change context, and it is facts only.** Per unit: risk `tags`
  from regexes over the changed lines (`_TAG_RULES`), `callers` from one `git grep` (POSIX
  classes, not `\b` — git's `-E` silently matches nothing on Perl escapes), `callers_in_tests`,
  `tests_in_change`, `siblings` (call links inside the change), `trivial`, and `risk` as a floor
  triage may raise. Per change: `signals` per wide pass and `lenses`. It writes `context.txt` and
  one `batch-N.txt` brief per craft agent so the orchestrator passes paths rather than transcribing.
  Nothing in those files is a finding or a hypothesis; that would anchor the independent reviewers
  the design exists to keep independent. Tags are deliberately over-inclusive — a tag costs one
  two-kilobyte lens file, a missed tag costs a blind spot.
- **Nothing a diff touched is silently dropped.** Changed lines outside any function become their
  own unit. A **deletion-only hunk** anchors at the line that now follows it, so a removed guard
  clause still produces a unit (`Hunk.new_lines`) — before that fix it produced nothing. Runs
  consisting only of blank lines and lone braces are filtered (`_has_substance`), and decorators
  and annotations are absorbed into the function they govern. **Mechanical edits are set aside,
  not dropped**: a unit whose every changed line is plumbing, or a rename applied at three or more
  sites (`_RENAME_MIN_SITES`), is listed under `trivial` with its reason and the report names it;
  `--include-trivial` reviews them. A hunk that both removes and adds is trivial *only* as a rename,
  never as "plumbing on both sides", because `int max = 5` → `int max = 7` is a changed decision.
- **The skill is read-only, and that is not negotiable.** No posting anywhere, no edits, no
  staging, no commits, no stashing, no formatters — by the orchestrator or by any agent it
  launches. It works in place; only MAKER gets a worktree, at the pre-change commit, removed
  afterwards. The only things it writes are under `/tmp/review-*`.
- **Scope resolution is explicit and reported.** `resolve_scope` returns the diff arguments *and*
  a human label, and the skill must print that label. The plan's `scope_args` goes to **every**
  agent that diffs, so ARCHITECT and the craft wave read the same change — an earlier version gave
  ARCHITECT only a file list, and it could diff a different scope than the pass beside it.
- **Every pass is a fresh subagent; never a `fork`.** The invoking session usually just *wrote*
  the code, holds the intent behind each decision, and will read the name it meant rather than
  the name that is there. The orchestrator does plumbing only — resolve, launch, reduce, report —
  contributes no findings of its own, and never passes the author's reasoning into an agent
  prompt. Craft, Architect and Integration never see each other's output.
- **Wave 0 is everything that does not depend on triage, launched together.** ARCHITECT,
  INTEGRATION, PO and MAKER's first step need nothing triage produces, so they launch alongside it
  (or instead of it). Sequencing them after the craft wave, as an earlier version did, put a whole
  agent's duration on the critical path for nothing.
- **Architect and Integration are conditional on the mapper's `signals`, and the report says why.**
  A one-file, one-function change with no structural or cross-file signal skips both; `--architect`
  / `--integration` force them on, `--no-…` off. **Triage is automatic**: it runs only when the
  plan has more batches than the craft budget, because that is the only time ranking changes what
  gets reviewed; `--triage` forces it for its per-unit risk judgement.
- **Integration owns cross-file behaviour and missing tests.** `charter/integration.md`. Craft is
  told to stay inside its function and Architect is told not to report bugs, so before this pass a
  contract changed under its callers, a write moved outside its transaction, or new behaviour with
  no test anywhere was nobody's job. Its bar is that every finding names **both sides** with
  `file:line`. It gets `context.txt` (the change map, callers, tests, coupling, signals) and the lens files the
  change's tags call for.
- **With no flags, the skill asks; it does not assume.** Step 4 uses `AskUserQuestion` to let the
  developer pick the passes and the depth, and it runs *after* the mapper deliberately: the free
  deterministic pass establishes the batch count and the signals, so the question carries real
  numbers and real reasons. Any explicit flag — or `--yes`, `--quick`, `--deep` — skips the
  question. Two rules that must survive any edit here: **selecting no passes stops the run**
  rather than falling back to the defaults, and the **resolved configuration is printed as one
  line before any agent is spent**.
- **Three passes by default, two opt-in, two presets.** Craft, Architect and Integration run
  (the latter two when signalled); `--maker` (two serial agents) and `--po` (mostly reports "not
  done yet" against mid-story work; the audit found AC findings the least-read category) are asked
  for. `--quick` and `--deep` are compositions of existing flags for two developer intents, not
  different reviews. `--max-agents` (default 12) is a hard ceiling with the wide passes taken off
  the top; a pass the signals switch off gives its agent back to craft. Posture is *not* a cost
  control: `--brutal` changes what the reducer keeps, not how many run.
- **The budget is spent in agents, and triage's selection is refilled.** Triage is told its budget
  in agents; the mapper packs its `selected` ids into file-pure batches in triage's order
  (`preserve_order=True`), cuts to `--max-batches`, and while agents remain fills them from
  `--fallback` (triage's deprioritised list, in order) via `refill`. An earlier version told triage
  "at most N units" where N was the agent budget and bypassed the batch budget entirely, which
  could only ever under-spend: ten units in three files cost three agents and left seven idle.
  `--high` marks the units triage judged high-risk so they batch nearly alone.
- **The review unit and the agent invocation are separate, and conflating them was the original
  design error.** `make_cards` produces **cards** — one coherent thing to judge, always inside one
  file — sized with no regard for cost: a `new-file` card (mostly-new file, read whole), a `suite`
  card (a test file; fixtures, setup, naming and duplication between cases are suite-level
  properties, so a single test function is almost never the right craft boundary), a `class` card
  (one class's changed methods), or a `region` card (module-level code). `pack_cards` then fills
  invocations, bounded by `INVOCATION_MAX_READ = 600`, `INVOCATION_MAX_CARDS = 6`,
  `INVOCATION_MAX_CHANGED = 150` and `INVOCATION_MAX_SYMBOLS = 12`. Cards carry coherence;
  invocations carry cost; neither budget is a measured cliff.
- **Why this matters is arithmetic, not taste.** Each craft agent reads ~48.5 KB of charter
  (`function_review` + `craft_lens` + `writing` + the language file + `candidates` + lenses) ≈ 12k
  tokens, paid *per invocation* and near-zero per card. On a large reference MR the old shape was
  76 agents ≈ 920k tokens of duplicated rubric alone, five times the cost of all its wasted file
  re-reading. So you want **many cards and few invocations**, which the old design made
  impossible by construction. That change measures 76 → **16 invocations**, 22,297 → **4,560
  lines read**, mean 285 and max 592 per agent, all 207 units still covered.
- **Cross-file invocations are now normal, and one prompt line is what makes them safe.** The
  brief states the cards are independent and that a finding must not span them. The register
  shift the never-span-files rule guarded against came from holding several files of *deep
  reading*, not from glancing at a few small targets — but if that line ever leaves the brief,
  the rule has to come back. `_coupled_order` still keeps call-linked units adjacent so a helper
  and its caller share a card.
- **Reading is bounded, and that bound is the one that was missing.** `read_lines_for` reads a
  file whole only at `WHOLE_FILE_FRAC = 0.5` or above, and otherwise takes each changed region
  plus `CTX_LINES = 40`. The craft prompt's old unconditional "Read the WHOLE file once" was
  outside every cap in the system, which is how one changed line in an 1,819-line test file cost
  a full-file read. **The total read is a property of the change, not of the grouping** — only
  the duplication was waste — so the budgets decide how it is sliced, never how much there is.
- **`_topup` exists because greedy packing under-fills.** Packing in directory order gives each
  agent nearby cards but cuts an invocation as soon as the next card does not fit and never
  revisits it, which left invocations of 17, 34 and 48 lines beside others near 600. The top-up
  pass best-fits the stragglers into whatever has room, so affinity still decides the bulk.
- **Production and test cards never share an invocation.** `select_batches` reserves agents for
  tests by `is_test`, and a mixed invocation makes that reservation and the report's split
  meaningless. Pairing a class with the suite that covers it — so the agent can see a production
  API forcing awkward test setup — is a real idea and a separate change.
- **Risk no longer shrinks a batch, but still composes the prompt.** `_RISK_CAP` is gone from the
  sizing path: for *craft* specifically, giving a money-tagged function fewer neighbours does not
  make a naming-and-maintainability reviewer better, and structural complexity is the more
  relevant signal. Tags stay load-bearing because they select the `lens_*.md` files. If a unit
  deserves more craft scrutiny, a second independent review is the right answer, not a smaller
  batch — and it waits for the benchmark.
- **Two ways to live within the budget, and they are not equivalent.** `select_batches` keeps
  invocations within their read budget and drops the overflow — part of the change reviewed well.
  `fit_cards` packs everything by relaxing the budgets — all of it reviewed less well. Measured on
  a large reference MR, natural size 16 invocations: `--fit 3` gives a mean of 13 cards and 1,520
  lines per agent, with a max of **26 cards and 2,822 lines**. `--fit` is right when the budget is
  near the natural invocation count and wrong at a fraction of it, and the "one card at a time"
  line in the craft prompt is an instruction, not a guarantee. `fit_cards` cannot go below two
  invocations on a change with both production and test cards.
- **The mapper reports the trade; it does not enforce it.** `card_stats` returns cards and lines
  read per invocation, and `_warnings` flags crowded invocations, an invocation whose single card
  busts twice the read budget, oversized *sliced* cards, and cards dropped. Both
  `BATCH_WARN_UNITS = 8` and `CARD_WARN_PER_INVOCATION = 6` are explicit **guesses**. The
  oversized count deliberately ignores `suite` and `new-file` cards: a suite's forty test cases
  are one review target, and counting them as crowding made the warning fire on nearly every
  change, which trains the reader to ignore it. The skill must repeat these warnings.
- **Production is selected before tests, with a quarter of the budget reserved for tests.**
  `_TEST_PATH` classifies; `select_batches` returns `(selected, skipped)` so the cut is reported.
- **Models are assigned per pass, not defaulted.** Craft, ARCHITECT and INTEGRATION on `opus`;
  MAKER triage on `haiku`; MAKER design on `fable`; MAKER compare on `opus`; PO and triage on
  `sonnet`. `--fast` / `--quick` drop craft to `sonnet` and the report must say so.
- **`charter/function_review.md` is the P1 craft pass, and `craft_lens.md` is its detail.**
  The defining fear is "can someone safely change this in three months?", *not* the 3am pager,
  and **craft findings are explicitly exempt from the "name a runtime failure mode or drop it"
  gate**. Applying it is exactly how function-level review regressed before.
- **The merge is a script, and it never judges.** `plugins/mr-review/scripts/merge_findings.py`
  parses each agent's output leniently, drops malformed findings, drops any finding whose quoted
  code is not in the cited file or the diff (whitespace-insensitive; a quote of a deleted line
  counts via `--diff`), dedupes exact repeats, collapses one title at many lines into one finding
  with `locations`, collapses same-locus findings to the higher altitude in every posture, folds
  craft symptoms into the design finding that explains them, and applies the posture's caps —
  never dropping a CRITICAL. An earlier version had the orchestrating model do
  these six steps by eye. **It must not grow a "is this finding right?" step**: the CLI's
  verification pass tried that ("re-evaluate strictly, be ruthless") and over-pruned craft, which
  has no fact a second model can check. The agreed shape for any future verification is
  *disprove-only, bug-kind CRITICAL/WARNING only, opt-in, withdrawals shown with the refuting
  line* — and it waits for the benchmark. Labels map to stages (`craft-3` → FUNCTION) when an
  agent omits `stage`.
- **Four scanner rules that look arbitrary and are not.** Each was a real defect found against
  a real repo, and each has a fixture in `--self-test`:
  1. **A found body is skipped wholesale** (`i = end + 1`), so a multi-line lambda stays
     attributed to its method.
  2. **A test-framework suite call is a container, not a function** (`_SUITE`); test cases are
     named from their label (`it: rejects an empty id`).
  3. **A name reached through a receiver is a call, not a declaration.** Only an *immediately*
     preceding dot disqualifies.
  4. **The container stack is pruned when `net <= 0 and "}" in line`.**
- **Both scripts' self-tests are the instrument.** `changed_functions.py --self-test` covers the
  scanner rules, plumbing and rename detection, deletion hunks, tags, coupling order and risk
  sizing; `merge_findings.py --self-test` covers parsing, the quote check, dedup, pattern collapse
  and the calm caps. With `tests/test_changed_functions.py` and `tests/test_merge_findings.py`
  they are the only thing that catches a regression in either, and a wrong span or a wrong drop is
  invisible in the output because everything still looks plausible.
- **The review is read on a local page, and the page stays local.** `render_review.py` joins the
  findings to the diff the way an MR view would — each card under its line, findings outside the
  hunks shown with working-tree context, deep links that open the file in VS Code or IntelliJ —
  and writes one self-contained file under `/tmp`. Publishing it as a claude.ai artifact was
  considered and **rejected as a default**: the page contains the repository's diff and quoted
  code, and a hosted URL is a different surface from the transient API processing the review
  already accepts. If sharing is ever added it is an explicit opt-in flag and a named exception to
  the skill's "never post anywhere" rule, never the default. The card labels (valid / invalid /
  duplicate) live in the browser per review title and export as JSON; they are the precision
  labels the benchmark needs, which is why the skill's report must mention the export. The SARIF
  and IntelliJ XML outputs exist so the IDE's own problems view can be used instead; a custom IDE
  extension was judged not worth its maintenance until the findings' shape has settled.
- **The next thing to build is a seeded-canary benchmark, and it unblocks the rest.** Take real
  code, deterministically inject the defects this pass is meant to catch — misleading names,
  duplicated conditionals, a helper acquiring several responsibilities, leaked internals,
  duplicated test setup — then vary lines read, cards per invocation, changed symbols, canary
  position and cohesion, and measure recall. **No model judge is needed, because the injection is
  known**, which matters here: the CLI's model-run verification pass over-pruned exactly the
  craft findings this review exists to produce. The hypothesis worth testing first is that recall
  falls with the number of distinct review targets rather than with raw lines. Until it exists,
  every budget constant above is a guess, and human review research (defect detection degrading
  past ~200–400 LOC per sitting) is at best an analogy — an LLM's failure mode is uneven use of a
  context it has room for, not fatigue.
- **What is deliberately not built yet, and why.** No golden-MR benchmark exists; the per-function
  pass's headline justification (twelve human findings, five from the batched pass, none matching)
  measured the *batched* pass and has not been re-run against the per-function one. Build the
  benchmark (a real MR with twelve human-labelled findings is the natural first fixture) before tuning batch sizes, adding
  verification, or reconsidering cross-file craft batches.

## Model and effort per pass

`EFFORT_BY_STAGE` in `mr_reviewer/reviewer.py` sets reasoning effort per pass; `--effort` /
`MR_REVIEWER_EFFORT` overrides every pass, and a stage absent from the map inherits the session
level. `DEFAULT_MODEL` is the one copy of the model id.

Design points to preserve when changing this:

- **The split is by whether a pass has a checkable answer, not by how important it feels.** PO,
  triage and the skill's MAKER triage get `medium`/`low` because a criterion is either evidenced or it is not,
  and a wrong answer is visible and bounded — triage's selection is refilled, PO's output is
  classified. ARCHITECT and `BLIND DESIGN/compare` get `high` because they derive a consequence from
  several facts, and because `merge_findings.py` is forbidden from judging whether a finding is
  right, so for them the generating model is the only filter.
- **SENIOR sits at `medium` on cost grounds, and that is a deliberate pin.** It is by far the most
  numerous pass, so its effort is the largest single lever in a run after invocation count.
  `tests/test_effort.py` asserts the value so raising it has to be a decision rather than a drift.
- **Nobody has compared Sonnet-at-high against Opus-at-medium for the same spend.** Every level
  here is a hypothesis; that comparison is the first thing the seeded-canary benchmark should
  settle, and until it runs do not treat the table as measured.
- **Omitting the flag must pass nothing.** A stage with no mapped level inherits the ambient
  session effort, so `_send_prompt` only appends `--effort` when it has a value. Defaulting it to
  a literal would silently override whatever the caller's session was set to.
- **An explicit override beats the per-pass level, not the other way round.** `self.effort or
  effort` in `_send_prompt`; the reverse would make `--effort max` a no-op on every mapped pass,
  which is every pass that matters.
- **`--effort` does nothing on the deprecated `--use-api` path,** which talks to the SDK rather
  than `claude -p`. Worth saying out loud rather than letting someone conclude the flag is broken.
- **The skill assigns models per pass but cannot assign effort.** The Agent tool takes no effort
  parameter, so `/review-changes` runs every pass at the session level. Per-pass effort there
  needs each pass declared as a subagent definition carrying `model` and `effort` in frontmatter —
  considered, and deliberately not built, because it trades eight more files for a setting the
  benchmark has not yet justified.

## Review posture, and the one reduce step

Every pass reviews at full depth and emits **candidates**; `plugins/mr-review/scripts/merge_findings.py`
then applies the run's posture. Three tiers, one table (`POSTURE_CAPS`), both consumers: `mr` (what
the CLI posts), `high` (the skill's default, and the CLI's under `--dry-run`) and `brutal`
(`--brutal` / `MR_REVIEWER_BRUTAL=1`, on either consumer). The CLI reaches the script
through `mr_reviewer/reduce.py`.

Design points to preserve when changing this:

- **Three entry points, and each exists for a different reason.** `reduce.reduce_for_mr` is what
  every CLI path should call: it owns the posture and the unit count, which its callers used to
  assemble themselves at seven sites between them. `reduce.cap_comments` applies *only* the
  posture's caps to findings already reduced once — learning mode's posting step and
  `--post-cached` both need that, and re-running the whole pipeline instead re-validated against a
  haystack the second call did not have and re-measured bodies `teach_in_comments` had just
  lengthened, so a finding lost a severity rank to its own teaching. `reduce.reduce_comments` is
  the full pipeline, with `posture` and `units` **required and keyword-only** — they were defaulted
  to different values at different layers, so one policy had two answers depending on where a
  caller entered.
- **`--post-cached` caps before it posts.** A dry run reports at `high`; posting that set unchanged
  put roughly three times the intended volume into a shared thread, on the very path this file
  tells people to prefer. It caps rather than re-reduces, because the findings cleared every drop
  rule when they were taken and re-validating them against a tree that has since moved would drop
  good ones for the wrong reason.
- **The tier is chosen by who reads the result, not by what the run cost.** `reduce.posture_for`
  takes `dry_run` and the brutal flag and nothing else. A posted comment spends the author's
  attention whether or not they agree with it; a dry-run report is read by the person who asked for
  it and nobody else. Using posture as a cost control would be the wrong lever entirely — the
  agents run either way, and `--quick`-style presets are what bound cost.
- **Detection never varies by posture, and that is the whole architecture.** `candidates.md` is
  appended to every CLI prompt on every run — it is not a brutal-mode file. A quieter *reviewer*
  loses subtle findings irrecoverably; a quieter *report* loses nothing, because the reducer records
  each suppressed finding with the cap that removed it. `brutal.md` used to be appended under the
  flag only, which meant a normal run asked its passes for a curated top-four and then ran a reduce
  step whose entire purpose was to do that curation with every pass in view. It is deleted.
- **There is exactly one place volume is decided.** The CLI's `STAGE_CAPS` / `_apply_stage_caps`,
  `dedupe_and_reduce` and `apply_global_cap` are gone. The old parse-time cap bound hardest and
  worst: it deleted findings as each pass was parsed, before dedup could tell it which of them were
  the same finding twice, and it is why lifting the later caps left brutal mode barely
  distinguishable from a normal run. What remains in `reviewer.py` is `RUNAWAY_GUARD = 15`, a
  malfunction guard for a pass that ignored its ranking instruction — it drops lowest-severity first
  and warns loudly, and it does **not** move with posture. **PO is exempt from it**
  (`GUARD_EXEMPT_STAGES`), because PO emits one comment per unmet acceptance criterion plus two
  roll-ups: fifteen of those is a correct pass, and a severity-ordered trim would cut exactly the
  two SUGGESTION-tier roll-ups the reduce step then protects from the global ceiling by name. The
  guard names the pass from the findings rather than from a parameter — its one caller does not
  know which stage it is parsing, and up to six passes run concurrently on the pool. `SEVERITY_RANK`
  in `reviewer.py` is now the single ordering of the three severities, which was written out four
  times.
- **Because there is one place, no caller has to remember the mode.** The three former reduce
  functions each re-read `brutal_mode()` themselves, defensively, because a caller that forgot the
  flag silently re-imposed a cap. That defence is no longer needed and `brutal_mode()` now has a
  single consumer: `posture_for`.
- **Every stage the CLI runs is in the shared table.** `_STAGE_PRIORITY` and the tiers' `stage`
  caps both carry `PROMPT`, which the CLI still runs and which the deleted `STAGE_CAPS` used to
  bound at 4. Unlisted, it fell to the `.get(stage, 9)` default: it lost every same-locus contest,
  any root in its file could absorb it, and nothing capped it — so a markdown-heavy MR's prompt
  warnings were bounded only by the runaway guard.
- **Absorption picks its root by altitude, and it is the one place that does.** `_rank` leads on
  severity, which is right for "which of these matters most" and wrong for "which of these names
  the decision": keyed on `_rank`, a CRITICAL ARCHITECT restatement out-ranked the MAKER design
  root and collected the symptoms itself. `absorb_symptoms._altitude` is the local reversal, and
  `collapse_locus` keeps severity-first because there a CRITICAL bug must not be replaced by a
  WARNING about the same line's shape.
- **Dedup and every drop rule stay on at all three tiers.** Two copies of one finding are not two
  findings. Quote or drop, the file check, no hedging, the bug-class evidence gates and every
  false-positive guard are identical in `mr`, `high` and `brutal` — the obvious failure of a "more
  findings" tier is more *wrong* findings, and a thread where a quarter of the comments are invented
  gets dismissed wholesale.
- **`preserve_all` is named for what it does, not for who asks.** It turns off symptom absorption
  and the caps while leaving dedup and every drop rule on. Learning mode and brutal mode want the
  same thing for different reasons — a lesson needs a pattern's second and third instance, a brutal
  review needs every symptom — so it is one flag, and the `brutal` posture implies it inside
  `reduce_candidates` rather than at the call sites. A posture with no ceilings that still folded
  symptoms into their root would be lifting the numbers while keeping the one lossy step that is
  not a number.
- **The writing bar is not lifted by any posture.** Demoting an over-proved body and keeping a
  300-word one off a diff line hide nothing — the finding is in the output either way — so there is
  nothing for `brutal` to lift.
- **Caps were never the binding constraint, and the docs must keep saying so.** The global ceiling
  has always refused to drop CRITICAL/WARNING and only ever trimmed optional SUGGESTIONs, and a
  measured run with every cap patched off returned 5 findings where a human made 12 comments. The
  bottleneck is what the prompts *generate*, which is why `candidates.md` is unconditional and why
  the per-function pass is the bigger lever for craft specifically.
- **Structural budgets are not caps and must stay.** `BATCH_BUDGET`, `PER_FILE_OVERHEAD`,
  `MAX_CONCURRENCY`, `HARD_FILE_CAP` and `PROMPT_REVIEW_MAX_FILES` bound *attention per call*, not
  output volume. Lifting them would make the review worse, not more thorough.
- **Brutal beats learning where they collide.** Learning mode returns the uncapped set for the study
  document and re-reduces at the posting step; under `--brutal` the posture *is* brutal, so the
  explicit request wins.
- **Diff mode reduces without a checkout.** `reduce_comments(..., repo_dir=None)` checks quotes
  against the diff alone and skips the file check, because dropping every finding for a file that is
  merely not on disk would silence the pass rather than filter it. It is weaker than clone mode and
  it is the best available on a path that never clones.
- **The reducer must reach the installed package.** `reduce.scripts_dir()` mirrors
  `prompts.charter_dir()`'s fallback chain — `$MR_REVIEWER_SCRIPTS`, the plugin beside the package,
  the pre-move location — and `pyproject.toml` includes `plugins/mr-review/scripts/*.py` in the
  wheel. Without the script an installed CLI has no reduce step at all.
- **Roots absorb symptoms; they never absorb bugs.** `absorb_symptoms` folds two or more
  lower-altitude, non-bug findings into the design or ARCHITECT finding above them, listing title
  and line. Three rules, each from a real MR: the locus is the enclosing **unit** when a plan is
  given (a design root in one method has no business absorbing a naming problem in another, though
  a root outside every unit — a class declaration — still owns its file); a root absorbs only what
  `_STAGE_PRIORITY` puts below it, so a design finding can absorb the ARCHITECT finding restating
  it, which on one real MR it could not; and a `bug`-kind or CRITICAL finding is **never** absorbed —
  it stays standalone and gains one line naming its root, because a data-loss bug must not be
  buried inside a design comment the author may disagree with.
- **Two findings from the same pass never collapse into each other.** `collapse_locus` decides the
  winner **per stage**: every finding from the winning pass survives, and only the other passes'
  framings of that line go. A pass reporting twice on one line has found two things, not one thing
  twice, and the repeated-title case is `collapse_patterns`' job. Deciding per stage rather than
  per arriving finding is also what makes the result independent of the order the passes finished
  in — worth keeping, because the passes run on a thread pool.
- **The "shared root tag" collapse is deliberately not built.** The assessment proposed also
  collapsing cross-pass findings whose normalised titles share a root tag. There is no definition
  of a root tag that does not guess, and a wrong collapse here deletes a true finding silently,
  which is the one failure this reducer must not have. Root absorption covers the cases the
  evidence actually showed.
- **The writing bar demotes and de-lists; it never rejects.** `apply_writing_bar` in the reducer:
  over `word_ceiling(f)` (150, or `STRUCTURAL_WORD_CEILING = 180` when `kind == "design"` or the
  stage is ARCHITECT) costs one severity rank; over `REPORT_ONLY_WORDS = 250` sets `inline: False`,
  which `main.py` folds into the summary note's `<details>` instead of posting on a line (through
  `_report_only_section`, which takes the threshold from the reducer rather than copying it). Both
  `writing` and `demoted_from` travel back onto the `ReviewComment` and are rendered by
  `render_markdown` and the HTML card, because they are the only record of why a finding wears a
  badge one rank below the one its pass gave it — without them a demotion reads as an unsure
  reviewer rather than a wordy one. The bar measures the body **as it stands**, not the count
  `validate` took at parse time: `collapse_patterns` and `absorb_symptoms` both append to a body
  after that, so a root that absorbed three symptoms was being measured at its pre-absorption
  length. It runs **after** the caps on purpose — a demotion before them could turn a wordy CRITICAL into a WARNING
  that a stage cap then trims, making length a reason a demonstrated bug disappears. Fenced code is
  excluded from the count, because a quote is evidence rather than verbosity.
- **`tests/test_posture.py` is the guard.** It pins the *relationships* rather than single counts —
  `mr` quieter than `high` quieter than `brutal`, the evidence bar identical at all three, the
  runaway guard indifferent to posture — because a volume setting fails silently in both
  directions. `merge_findings.py --self-test` and `tests/test_merge_findings.py` cover the numbers.

## Lint, format and types

`ruff` is both the linter and the formatter, `mypy` is the type checker, and both gate a commit
through `.pre-commit-config.yaml` alongside gitleaks. Install the hooks with `pre-commit install`.

Design points to preserve when changing this:

- **One copy of each tool, and it is the one in `poetry.lock`.** The hooks are `system` hooks
  pointing at `.venv/bin/ruff` and `.venv/bin/mypy` rather than pre-commit's pinned mirrors, so
  the version that gates a commit is the version `poetry run` gives you. A mirror would be a
  second pin to keep in step, and the failure mode is a commit that passes locally and fails in
  the next clone.
- **`ruff format` replaced black, which had been configured and never run.** Twenty of
  twenty-two files were unformatted, so the catch-up is one commit
  (`ff2ec4c`, listed in `.git-blame-ignore-revs`) and `git blame` should be pointed at that file:
  `git config blame.ignoreRevsFile .git-blame-ignore-revs`.
- **E501 is off on purpose.** The formatter owns line width, so E501 only fires where it declined
  to split — and in this codebase that is nearly always a long string: prompt text, a charter
  path, a user-facing message. Hand-wrapping those is a semantic edit to the exact strings the
  review's behaviour depends on, for a cosmetic gain.
- **mypy runs over the whole tree, not the staged files.** It infers across modules, so a file
  checked alone both misses errors at the seams and invents ones about imports it cannot see.
  The tree is `mr_reviewer`, `plugins/mr-review/scripts` and `tests`, and it is clean — a new
  error is something this change introduced.
- **Two tests read call sites out of the source, and they are spelling-agnostic for a reason.**
  `tests/test_effort.py` and `tests/test_posting.py` parse `main.py` and `orchestrator.py` with
  `ast` to check which pass labels and which kwargs names appear. They accept `dict(a=1)`,
  `{"a": 1}` and an annotated assignment of either, because a mechanical rewrite of a bundle must
  not read as a behavioural failure. `review_kwargs` is annotated `dict[str, Any]` precisely
  because a heterogeneous splat cannot be type-checked — that test is the check instead.

## Keeping docs in sync

**Any change to the reviewer's behaviour must update `README.md` in the same commit.** The README is
the user-facing contract and it drifts silently — it described "one Claude session, three passes
(PO → Architect → Principal)" for months after the orchestrated pass team shipped, and still
recommended the deprecated `--use-api` path as a peer to clone mode.

Update the README when a change touches any of these:

- **A pass is added, removed, renamed, or re-scoped** → the Features list, the "How a Single MR Is
  Reviewed" mermaid diagram, and the per-pass lens table under "Review Focus Areas".
- **A CLI flag or env var is added or changed** → the options tables and the Usage examples. Check
  them against `mr-reviewer review --help` and `review-all --help` rather than from memory.
- **A posture tier, a cap, a budget, or any reduce-step rule changes** → `POSTURE_CAPS` is the one
  table and both consumers read it, so the change lands on the skill and the bot at once. Update
  the notes under the flow diagram, the posture table under "Review posture", and "Comment
  Severity Levels" if protection or rendering changed.
- **A prompt file is added** → say what reads it and when. It goes in
  `plugins/mr-review/charter/`, and if the skill reads it too, name it in the skill's pass list.
- **The skill's passes, flags, models, or merge rules change** → `plugins/mr-review/skills/review-changes/SKILL.md`
  *and* the README's "Reviewing from a Claude session" section. The skill file is itself
  user-facing documentation, so a behaviour change that only edits the Python leaves the skill
  describing a review it no longer runs.
- **The charter moves, or `charter_dir()`'s fallback chain changes** → the Project Structure list
  above, the README's Features entry, and `pyproject.toml`'s `include` (a built wheel resolves the
  charter through that path).
- **Call count or runtime changes materially** → "Cost and runtime". That section is about passes and
  wall-clock, not token pricing, because clone mode bills against a subscription.
- **Something becomes deprecated** → mark it in place. Do not leave a deprecated path documented as a
  normal option.

This file (`CLAUDE.md`) covers *how the code is organised* for someone changing it; the README covers
*how to run it* for someone using it. A behavioural change usually needs both.

## Logs

- Reviews log to stdout/stderr (run on demand; redirect to a file if you want a record)
- Summary line format: `REVIEW SUMMARY -- N reviewed, N skipped, N errors (Ns total)`
- Skipped MRs are expected (already reviewed or no changes since last review)
