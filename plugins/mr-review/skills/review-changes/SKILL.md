---
name: review-changes
description: Review your own local code changes against the mr-review charter — before the MR exists. A script maps the diff to changed functions, groups them into coherent review cards, and packs the cards into agent invocations by read budget; isolated subagents then review it from four angles — craft, whole-change shape, cross-file integration, and optionally an independent design and the story's acceptance criteria — and a deterministic reducer merges the findings. Read-only: it never posts anywhere and never edits your code. Use when asked to review my changes, review this branch, review before raising an MR, or check my work.
argument-hint: "[--staged|--uncommitted|--base <ref>] [--quick|--deep] [--plan] [--max-agents <n>] [--maker] [--po] [--brutal] [--fast] [--only <passes>] [--files <paths>]"
allowed-tools: Bash, Read, Glob, Grep, Agent, AskUserQuestion, mcp__claude_ai_Atlassian__getJiraIssue, mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql
---

# Review changes

Review the work in this repository the way the mr-reviewer bot would review it — but **now**, on
the local branch, while the feedback is still cheap to act on. There is no merge request
involved: by the time one exists the code is already someone else's problem, and the useful
moment for this review is before that.

The charter this reviews against is the same one the `mr-reviewer` CLI uses. What differs is the
**unit of attention** and the **division of labour**. Each reviewer here answers one question,
and the questions do not overlap:

| Pass | The question it answers |
|---|---|
| **Craft** | Is the implementation locally good? Changed code is grouped into coherent **review cards** (a new file, a test suite, a class's changed methods), and cards are packed into agent invocations by read budget. |
| **Architect** | Is the structure good? Class and module responsibilities, boundaries, abstractions that do or do not earn their place. |
| **Integration** | Does the change behave correctly as a system? Contracts under their callers, behaviour spread across files, transaction and failure paths, missing tests. |
| **Design** (MAKER) | Was this the right solution in the first place? Designs the change from the story alone, blind to the diff, then compares. Needs a story. |
| **Acceptance criteria** (PO) | Does it do what the story asked? |

A **script** decides the facts before any agent runs — which functions changed, their exact
spans, who calls them, whether anything tests them, which of them call each other, and which
edits are mechanical — and a **script** merges the findings afterwards. Agents spend their
attention on the code, not on bookkeeping, and the two steps that must be exact are exact.

## Scope and flags

| Flag | What gets reviewed |
|---|---|
| *(none)* | This branch's work since the trunk branch, **including uncommitted changes** |
| `--staged` | Only what is staged |
| `--uncommitted` | Only what is not committed yet (staged and unstaged) |
| `--base <ref>` | Everything since `<ref>`, including the working tree |
| `--base <a> --head <b>` | A committed range |
| `--files <paths>` | Restrict to these paths, within whichever scope applies |
| `--also-repo <path>` | A second repository the same change touches. Its diff is **not** reviewed here — its own run does that — but it is searched for callers and tests of this change, so a contract whose callers all live there stops reading as a contract with no callers. Repeatable. |

**Do not interpret these yourself.** Pass them through to the mapper verbatim wherever a step
below writes `<scope flags>` — it owns them, its `--help` documents them, and re-implementing
their precedence in prose is how the two would drift apart.

### Passes — three on by default, two opt-in, one automatic

Cost is the reason for this split. Every pass is a real agent, and a default that runs everything
means the cheap question ("is my branch alright?") costs the same as the thorough one.

| Flag | Pass | Default | Cost |
|---|---|---|---|
| `--no-craft` | **Craft** — per-function maintainability, one agent per file batch | **on** | 1 agent per batch |
| `--no-architect` / `--architect` | **Architect** — whole-change class and boundary shape | **on when the change has shape** (see below) | 1 agent |
| `--no-integration` / `--integration` | **Integration** — cross-file behaviour, contracts under callers, missing tests | **on when there is something to integrate** (see below) | 1 agent |
| `--maker` | **Design** — designs an alternative, then diffs it against what was built | off | **2 serial agents** |
| `--po` | **Acceptance criteria** — needs a Jira story | off | 1 agent + a Jira fetch |
| `--only <names>` | Run only these: `craft`, `architect`, `integration`, `maker`, `po` | — | — |

**Architect and Integration are conditional, and the mapper decides.** Its plan carries a
`signals` object with a list of reasons per pass. Architect runs when `signals.architect` is
non-empty (more than one production file changed, a new file declaring a class or interface, a
widely-called signature changed, a design-decision signal such as a new abstraction or a
suppressed quality gate). Integration runs when `signals.integration` is non-empty (more than one
production file, a public contract changed, a persistence, concurrency or external-boundary tag,
a changed function nothing tests, changed functions calling each other across files). A one-file,
one-function change with none of these skips both passes and **the report says so, with the
reason**. `--architect` or `--integration` forces the pass on regardless; `--no-…` forces it off.

Why the two chains are opt-in rather than on:

- **Design (MAKER) is two serial calls and needs a story**, which makes it the longest chain in
  the review and the only pass that can cancel itself. It earns its cost when a change involved a
  real choice of mechanism, and it is worthless without acceptance criteria good enough to design
  from. When `signals.maker` is non-empty the report says so and suggests `--maker`, rather than
  running it.
- **Acceptance criteria on uncommitted work is usually noise.** Mid-story, half the criteria are
  legitimately unmet, and a pass that reports "not done yet" about work in progress tells the
  developer nothing they did not know. It becomes useful once the branch is complete.

**Triage is automatic, not a pass you choose.** It runs only when the plan has more craft invocations
than the budget allows — that is the only time ranking changes what gets reviewed — and it is
skipped, and reported as skipped, when everything fits. `--triage` forces it on (for its
per-unit risk judgement on a change that fits); `--no-triage` forces it off and the mapper's
own ordering (production first, then risk tags, then volume) decides.

**Resolving the flags — do this first, and state the result.** With no flags at all, Step 4 asks
the developer which passes to run and the defaults above are only what is *recommended* there.
When any flag is present the question is skipped. If `--only` is present it *replaces* the
default set entirely with the passes it names, and any `--no-<pass>` then subtracts from it; a
contradiction such as `--only craft --no-craft` leaves nothing, which is an error rather than an
empty review. Print the resolved set as one line — `passes: craft, architect, integration
(triage: if needed, posture high)` — before spending a single agent, because a flag that
silently did nothing is worse than one that was rejected.

If every pass ends up off, say so and stop rather than producing an empty report.

### Posture — how much of what was found gets reported

| Flag | Posture | Threshold | Caps |
|---|---|---|---|
| *(default)* | `high` | Everything a senior would raise while pairing — bounded by what you can work through | 4 craft per file, 12 architect, 10 integration, 4 design, global ceiling 14–40 by unit count |
| `--brutal` | `brutal` | Everything that clears the evidence bar | none |

There is a quieter `mr` tier in the same table, and this skill never uses it. That tier exists for
the CLI, which posts into a shared thread where every comment spends someone else's attention; this
review runs on a local branch *before* the merge request exists, nothing is posted anywhere and
nobody else reads it, so that etiquette simply does not apply. `high` is therefore generous rather
than selective: the only real constraint is that you can get through the list and act on it,
because one nobody finishes is worth less than a shorter one they do. Do not offer `--calm` or an
`mr` posture here — a developer reviewing their own branch asked to see the work, and the tier
would only hide findings nobody else was ever going to read.

**The evidence bar is identical in both.** A bug still needs a concrete input and a concrete
wrong result; a security finding still needs an attack path; a concurrency finding still needs
named shared state and a sequence. Posture moves the *craft* threshold and the ceilings, never the
standards, so a finding `high` suppressed was true and merely not worth a pairing partner's
attention.

**Detection is separate from presentation, and that separation is the point.** Every agent runs at
full depth in every posture and emits candidates; the numbers live in `merge_findings.py`, not in
the prompts. Making a *reviewer* quieter would cost you the subtle findings this review exists to
produce, whereas making the *report* quieter costs nothing — the suppressed ones are all in the
merged JSON's `dropped` array with the cap that removed them. So posture is not a way to save
money either: it changes what the reducer keeps, not how many passes run.

**The global ceiling scales with review units, not with diff size.** A two-thousand-line rename
carries no design freedom and a forty-line one often carries all of it, so the ceiling follows the
number of units that survived triage, sublinearly and bounded: 14 at one unit, 22 at nine, 30 at
twenty-five, 40 at the top. Twice the units is not twice the useful comments, and 40 is about
where a list stops being worked through and starts being skimmed.

### Cost, and the two presets

| Flag | Effect |
|---|---|
| `--quick` | **Pre-commit look.** Craft on `sonnet`, no triage, no architect, integration only if a persistence, concurrency, boundary or public-contract signal fires, at most 4 agents. Skips the question. |
| `--deep` | **Before a risky merge.** The default passes plus `--maker`, budget 20. Skips the question. |
| `--plan` | Show the plan and the agent count, then **stop**. Spends one cheap agent at most, and none when triage is not needed |
| `--max-agents <n>` | Total agent budget, default **12** |
| `--fast` | Run the craft pass on `sonnet` instead of `opus` |
| `--fit <n>` | Review **everything** in at most `n` agents by relaxing the invocation budgets, instead of keeping invocations small and skipping the overflow |
| `--include-trivial` | Review mechanical edits (imports, declarations, renames) too, instead of listing them as set aside |
| `--yes` | Skip the interactive question in Step 4 and use the defaults |

The presets are compositions of the flags above, not different reviews. `--quick` answers "did I
just break something obvious?" in a couple of minutes; the default answers "is this branch
alright?"; `--deep` adds the one pass that can say "you built the wrong thing".

### Depth or coverage — the choice the budget forces

When the natural batch count exceeds the budget, there are two honest things to do and they are
not equivalent:

- **Depth (the default).** Keep invocations within their read budget, let triage rank, review the top of the list
  properly, and **report the rest as not reviewed**. Part of the change is reviewed well.
- **Coverage (`--fit <n>`).** Pack every card into `n` agents by relaxing the budgets. All of the
  change is reviewed, less well.

The second degrades fast, because invocation size scales with the ratio. Measured on a real
change (a large reference MR: 68 files, 207 changed functions, 38 review cards) whose natural size
is 16 invocations:

| Budget | Agents | Cards per agent (mean / max) | Lines read per agent (mean / max) |
|---|---|---|---|
| `--fit 3` | 3 | 13 / **26** | 1520 / **2822** |
| `--fit 5` | 5 | 8 / **14** | 912 / **1974** |
| `--fit 10` | 9 | 4 / 10 | 507 / 1023 |
| *(no `--fit`)* | 16 | 2 / 5 | 285 / 592 |

At three agents one agent reads 2,800 lines and holds 26 independent targets, which is precisely
the shape that returned five findings where a human found twelve. So: **`--fit` is right when the
budget is close to the natural invocation count**, and wrong when it is a fraction of it. The mapper enforces none of this; it *reports* it, and **you repeat its warnings to the
developer** — they are the difference between a stated trade and a silent one.

**Do the arithmetic out loud before launching anything, and show it.** The wide passes come off
the budget first, and craft invocations get the remainder:

```
default (craft + architect + integration):  [1 triage if needed] + 1 architect + 1 integration + craft remainder  = up to 12
with --maker:                               + 2 design
with --maker --po:                          + 2 design + 1 po
--only craft --max-agents 6:                [1 triage if needed] + craft remainder
--quick:                                    craft on sonnet [+ 1 integration if signalled]      = up to 4
```

A pass the signals switched off gives its agent back to craft. Pass the craft remainder to the
mapper as `--max-batches`. A typical branch produces two to five invocations, so the budget
usually does not bind — but on a large branch it does, and then the plan's `skipped` list is what
you report. If the plan comes back with more invocations than the budget allows, **say the number
before launching** and offer `--plan`, a narrower scope (`--staged`, `--files`), or a higher
`--max-agents`.

---

## STEP 1 — Locate the charter

Everything below depends on the charter files, so fail fast if they are missing.

```bash
PLUGIN=$(bash "${CLAUDE_PLUGIN_ROOT:-.}/scripts/plugin_root.sh" 2>/dev/null) || PLUGIN=""
[ -f "$PLUGIN/charter/base.md" ] && ls "$PLUGIN/charter/"
```

If that produced nothing, resolve `$PLUGIN` as the directory two levels above this `SKILL.md`
(this file sits at `<plugin>/skills/review-changes/`) and check again. If it still fails,
**stop** and say the plugin install looks incomplete. Do not review from memory of the rules —
the charter is the review.

## STEP 2 — Establish where you are

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short | head -30
```

Two things to check before going further, because both change what the review means:

- **Are you in the repository the user means?** A multi-repo workspace holds many repos side by
  side. If the toplevel looks wrong for what they asked, say so and stop.
- **Does the change reach past this repo?** A workspace repo rarely changes alone, and the
  half of a change you cannot see is where it breaks: each side is internally consistent, so
  both reviews come back clean while the seam between them is wrong. Pass every other repo
  carrying this branch as `--also-repo <path>`. If `<repo>/.vicaris/repos-<branch>.txt`
  exists (or `<repo>/.dev-flow/repos-<branch>.txt`, its name before that plugin was
  renamed), that file is the set — read it and pass every entry but this one. Otherwise look
  for sibling directories on the same branch:
  ```bash
  BRANCH=$(git branch --show-current)
  for d in ../*/.git; do r=${d%/.git}; [ "$(git -C "$r" branch --show-current 2>/dev/null)" = "$BRANCH" ] \
    && [ "$(git -C "$r" rev-parse --show-toplevel)" != "$(git rev-parse --show-toplevel)" ] && echo "$r"; done
  ```
  Name what you found in the report, including "this change is in one repo" when it is —
  a cross-repo review that silently ran single-repo is the failure worth being loud about.
- **Is there anything to review?** If the scope turns out empty in Step 3, say so plainly
  rather than reviewing the last commit as a consolation prize.

**Then open a run directory, and use it for everything this run writes.**

```bash
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/review-$(basename "$(git rev-parse --show-toplevel)")-XXXXXX")
echo "$RUN_DIR"
```

Every path below is written relative to it — `$RUN_DIR/briefs`, `$RUN_DIR/findings`,
`$RUN_DIR/plan.json`, `$RUN_DIR/review.diff` — and **you substitute the real path into every
agent prompt**, because a subagent cannot read your shell variables.

This used to be a set of fixed paths under `/tmp` that the run cleared on entry, and that made
two reviews mutually destructive in the two cases where they most often overlap. Reviewing two
repositories of one cross-repo change means two runs of this skill, and an orchestrator that
launched them together had the second wipe the first's briefs mid-flight — so agents reviewing
repo A read repo B's change context and reported findings against code that was never in their
diff. The second case is quieter: an orchestrator like Vicaris gives every repo of a change
**the same branch name**, so even the branch-slugged report path collided, and the HTML for the
last repo reviewed overwrote the rest. A directory per run removes both, and it removes the
`rm -rf` that made a concurrent run destructive rather than merely confusing.

## STEP 3 — Build the plan and the change context (free — no agents yet)

Run the mapper **unbudgeted** first, writing its briefs. The budget depends on which passes the
developer picks in Step 4, and this run exists to tell them how big the change actually is — so
it must show everything, and it costs nothing because no model is involved.

```bash
mkdir -p "$RUN_DIR/briefs" "$RUN_DIR/findings"
python3 "$PLUGIN/scripts/changed_functions.py" <scope flags> --max-batches 0 --json \
  --briefs-dir "$RUN_DIR/briefs" > "$RUN_DIR/plan.json"
python3 "$PLUGIN/scripts/changed_functions.py" <scope flags> --max-batches 0
```

`<scope flags>` includes an `--also-repo <path>` for every sibling Step 2 found, whether the
developer named it or the manifest did. Leaving one out is not a smaller review, it is a review
that reports a changed contract as having no callers.

The JSON carries:

- `scope` and `scope_args` — the human label and the exact `git diff` arguments. **Report the
  label**, and hand `scope_args` to every agent that diffs, so all of them read the same change.
- `units` — every substantive changed function, plus runs of changed lines outside any function.
  Each carries its `tags` (persistence, concurrency, authz, money, retry_cache, boundary,
  public_api), `risk`, `callers`, `tests_in_change`, `untested`, and `siblings` (other changed
  functions it calls or is called by). These are facts from greps: leads for the agents, not
  findings.
- `trivial` — units whose every changed line is plumbing or a rename applied throughout. The
  mapper **sets these aside** and lists them with the reason; they are not batched unless
  `--include-trivial`, and the report names them under "not reviewed".
- `cards` (inside each batch) — the **review units**: one coherent thing to judge, always
  within one file. `kind` says why they are one card — `new-file` (a new artifact, read whole),
  `suite` (a test file, judged as a suite), `class` (one class's changed methods), `region`
  (module-level changed code). Cards are sized for coherence with no regard for cost.
- `batches` — the **agent invocations**: however many cards fit one agent's budget, bounded by
  `read_lines`, card count, changed lines and changed symbols. An invocation may span files;
  that says nothing about the cards belonging together, and the brief tells the agent so. One
  craft agent per batch. Its length is the invocation count you quote in Step 4's question.
- `read_lines` — lines of code an invocation must actually read. This is the number that
  drives cost, and it is why a one-line change in a 1,800-line file no longer costs a
  whole-file read.
- `signals` — per pass, the reasons it has something to look at (see "Passes" above). Empty
  means skip.
- `siblings` — the repositories searched besides this one, with the label each uses to
  prefix a call site. Empty when the change is single-repo.
- `lenses` — the lens charter files the change's tags call for.
- `briefs` — `context.txt` (the whole change context, for the wide passes) and one
  `batch-N.txt` per batch (that batch's brief, for its craft agent). **Hand agents these paths
  rather than transcribing unit lists into prompts.**
- `skipped`, `refilled`, `warnings`, `counts` — coverage bookkeeping for the report.

If this comes back with no units and no trivial units, stop here and say so — there is nothing
to review and no question worth asking. If it comes back very large, that is exactly the number
Step 4 needs in front of the developer before they choose a budget.

Capture the diff for the wide passes too, with the same scope:

```bash
git diff <scope> --stat
git diff <scope> > $RUN_DIR/review.diff && wc -l $RUN_DIR/review.diff
```

Pick the language charter from the dominant changed extension: `.java`/`.kt` → `java.md`,
`.ts`/`.tsx` in an Angular repo → `angular.md`, React or Next → `react.md`, `.py` →
`python.md`, otherwise `generic.md`.

## STEP 4 — Ask what to run

You now know the real size of the change and nothing has cost anything yet. **Ask the developer
what to run, with the numbers in front of them.**

**Skip the questions entirely when nobody is there to answer them.** An autonomous run — Vicaris, a hook, anything with no one watching the terminal — must never reach an
`AskUserQuestion`, because a prompt waiting on a keypress stalls the whole run for as long as
the developer is away, which is the stretch they left it running for. Take the recommended
answer to both questions below — the pass set the signals imply, `Standard` depth — print the
resolved line, and go. The same holds whenever asking would cost more than a wrong default:
the defaults are recommended because they are right most of the time, and a review that ran
with one pass too many beats one that never ran.

**Skip them too when the flags already decided it** — if any of `--quick`,
`--deep`, `--only`, `--maker`, `--po`, `--no-craft`, `--no-architect`, `--architect`,
`--no-integration`, `--integration`, `--no-triage`, `--triage`, `--brutal`, `--fast`, `--plan`,
`--max-agents`, `--fit` or `--yes` was passed, the developer has expressed a preference and being
asked again is friction. Resolve the flags as described above, print the resolved line, and go.

Otherwise use `AskUserQuestion` with these two questions, in one call. Lead each option list with
the recommended choice and mark it, so accepting the defaults is one keystroke:

**Question 1 — "Which review passes?"** (`multiSelect: true`, header `Passes`)

| Option | Description to show |
|---|---|
| `Craft — maintainability review (Recommended)` | Changed code grouped into coherent review cards and packed into agents by read budget. `<N>` cards in `<M>` agents for this change. |
| `Architect — whole-change shape (Recommended if signalled)` | Class and module responsibilities, boundaries, abstractions. 1 agent. Show `signals.architect` in one line, or "no structural signals — would likely return nothing" when empty. |
| `Integration — does it work as a system (Recommended if signalled)` | Contracts under their callers, behaviour across files, transactions and failure paths, changed functions with no test. 1 agent. Show `signals.integration` in one line, or "single-file change with no callers — would likely return nothing". |
| `Design — build an alternative, then compare` | Designs the change independently at the pre-change commit, then diffs its design against yours. The only pass that can catch a wrong *decision*. 2 serial agents. Mention `signals.maker` when non-empty. |

Add a fifth option, `Acceptance criteria — check against the story`, only when a Jira key was
found in the branch name; otherwise say in the report that none was found.

**Question 2 — "How deep, and what should it cost?"** (single select, header `Depth`)

| Option | Description to show |
|---|---|
| `Standard — Opus, up to 12 agents (Recommended)` | Full judgement on every pass, `high` posture: what a senior would raise while pairing. |
| `Quick — Sonnet craft, up to 4 agents` | Roughly a quarter of the cost and blunter findings. A pre-commit look, not a review. |
| `Cover everything — pack all functions into the budget` | Every changed function rather than the riskiest subset. State the cost: `<N>` units across `<B>` agents means about `<N/B>` functions per agent, and beyond about eight the per-function depth stops being real. |
| `Plan only — show me what it would review` | Prints the ranked plan and the agent arithmetic, then stops. |

Map the answers onto the flags: the selected passes become the resolved pass set, `Quick` means
`--quick`, `Cover everything` means `--fit <craft budget>`, `Plan only` means `--plan`.

**When the plan from Step 3 has more than twice as many invocations as the budget allows, say so
in the question itself** — put the real numbers in the depth options, as in "24 batches, budget 9:
half of this change gets no craft agent".

Three rules about this step. **A selection of no passes is an answer** — say that nothing was
selected and stop, rather than falling back to the defaults. That is the one case where running
nothing is right, and it needs someone to have actually chosen it: an unanswered question is not
a selection of nothing, it is a question that should not have been asked. **Never ask twice in
one run**; if the answers are unusable, state your interpretation and proceed. And **print the resolved
configuration as one line before spending an agent**, whether it came from flags or from
answers:

```
passes: craft (38 cards in 16 agents), architect, integration · triage: skipped (fits) · posture: high · craft model: opus · budget: 12 agents
```

## STEP 5 — Find the story (only if the acceptance-criteria pass is selected)

Look for a ticket key in the branch name first, then recent commit messages on this branch. If
`$JIRA_PROJECT_KEYS` is set (comma-separated, e.g. `PAY,CORE`), only those keys count; otherwise
match `[A-Z][A-Z0-9]+-[0-9]+`, and ignore obvious non-tickets such as `SHA-256` or `UTF-8`:

```bash
git branch --show-current
git log --format=%s <base>..HEAD 2>/dev/null | head -10
echo "${JIRA_PROJECT_KEYS:-}"
```

If one is found, fetch it with whichever Jira tool this session has. `$JIRA_MCP_TOOL` names it when
set; otherwise use the Atlassian connector's `getJiraIssue` (`mcp__claude_ai_Atlassian__getJiraIssue`,
or a suffixed variant such as `mcp__claude_ai_Atlassian_2__getJiraIssue` when the account has more
than one connection), falling back to its `searchJiraIssuesUsingJql` with `key = <TICKET>`. Keep the
summary, description and acceptance criteria.

If no ticket is found, or the fetch fails, **skip the PO pass entirely** and say so in the
report. A PO pass with no story invents acceptance criteria, which is the worst output this
review can produce.

## STEP 6 — Run the passes

### Why every pass is a subagent

Each pass runs as its own Agent with a **fresh context**, and that isolation is the point, not
an implementation detail.

The session invoking this skill is usually the session that just *wrote* the code. It holds the
intent behind every decision, the constraint that made the awkward bit necessary, and the
reasoning that already justified the shortcut. A reviewer holding all that cannot see the code
as the next developer will see it — it will read the name it meant rather than the name that is
there, and it will excuse the confusing branch because it remembers why the branch exists. That
is precisely the failure this review exists to catch, and a reviewer sharing the author's
context cannot catch it.

A fresh agent gets the charter, its brief, and the repository. No design discussion, no
justifications, no memory of what the author intended. It reads what a stranger would read.

So, concretely:

- **Use `subagent_type: "general-purpose"`** (or omit it). Every pass is a fresh agent.
- **Never use `subagent_type: "fork"`.** A fork inherits the calling session's full context,
  which reintroduces exactly the contamination this design removes.
- **Never review in the orchestrating session.** Your job here is plumbing: resolve the scope,
  launch the agents, run the reducer, report. You do not add findings of your own, you do not
  "improve" a finding with context the agent did not have, and you do not drop one because you
  happen to know why the code is like that. If you think an agent missed something, say so as
  your own observation in the report, labelled as yours.
- **Do not pass the author's reasoning into an agent prompt.** The briefs are facts — spans,
  changed lines, callers, tags, coupling. Adding "the user says this is temporary" would defeat
  the isolation one sentence at a time.
- **Independence between passes is deliberate.** Craft, Architect and Integration never see each
  other's output. The reducer sees everything, and it is a script.

### Which model runs which pass

Pass the `model` parameter explicitly on every Agent call.

| Pass | Model | Why |
|---|---|---|
| Craft (Wave A) | `opus` (`sonnet` with `--fast` or `--quick`) | The hardest judgement in the review. Naming a smell precisely, and holding the line between a real craft finding and a nitpick, is where a weaker model produces confident noise. |
| ARCHITECT | `opus` | Whole-change shape, boundaries, whether an abstraction earns its place. Fewest calls, most leverage per call. |
| INTEGRATION | `opus` | Has to hold the change and its callers, tests and boundaries together and name a concrete sequence that goes wrong. |
| MAKER — design | `fable` | This step *designs* from the story, it does not review. Planning work, on the house planning model. |
| MAKER — compare | `opus` | Holds the frozen design and the real implementation side by side and names what the divergence costs. |
| PO | `sonnet` | Structured verification: walk the criteria, find the evidence, classify. |
| Triage | `sonnet` | Ranking risk needs judgement but not the deepest kind, and it only runs when ranking changes what gets reviewed. |

`--fast` and `--quick` move the craft pass to `sonnet`. Do not quietly downgrade without saying
so: report which model reviewed the functions, because it changes how much weight the findings
deserve.

Have each agent **read the charter files itself** rather than pasting them into its prompt, and
hand it its brief by path.

Every agent prompt must carry this line, because these agents run in the user's live working
tree rather than a throwaway checkout:

> This is a review. Do not edit, create, delete, stage, commit or stash anything. Use Read,
> Grep, Glob and read-only `git` commands only.

### Wave 0 — everything that does not depend on triage, launched together

Nothing here waits for anything else, so send all of it in **one message with multiple tool
calls**: triage (if needed), ARCHITECT, INTEGRATION, PO, and MAKER's design step once its brief
is built. Sequencing
the wide passes after the craft wave, as an earlier version of this skill did, put an entire
agent's duration on the critical path for no reason.

**Triage** — `model: "sonnet"`, only when `counts.batches_total` exceeds the craft budget (or
`--triage`). It decides **which units are worth a reviewer and in what order**, the judgement
the mapper cannot make.

```
You are triaging a change: deciding which changed functions deserve a reviewer, not reviewing.

Read these in full first:
  - $PLUGIN/charter/triage.md          (your criteria and your output format)
  - $RUN_DIR/briefs/context.txt     (the change context: every unit, its tags, callers, tests)

Repository: <repo toplevel>
Budget: <craft budget> agents. One agent reviews several coherent review cards that fit its
read budget; a card is a new file, a test suite, or one class's changed methods.

The full diff is at $RUN_DIR/review.diff — read it. Use Read, Grep and Glob to check anything you
are unsure about.

The changed functions, as identified by a parser (do not re-derive these), are listed in the
context file with their ids. Mechanical edits are listed separately and are already set aside.

This is a review. Do not edit, create, delete, stage, commit or stash anything.

Output the single JSON object specified in triage.md and nothing else.
```

Then feed its ranking back to the mapper, which re-groups the chosen units into file-scoped
batches in triage's order, **spends the budget in agents**, and fills any agents left idle from
the top of the deprioritised list:

```bash
python3 "$PLUGIN/scripts/changed_functions.py" <scope flags> --json --briefs-dir $RUN_DIR/briefs \
  --max-batches <craft budget> \
  --select <selected ids, in order> --high <ids triage marked high> \
  --fallback <deprioritised ids, in order> > $RUN_DIR/plan.json
```

Handle two failure modes rather than trusting the output: if triage returns unparseable JSON or
no `selected`, fall back to the mapper's own budgeted ordering (`--max-batches` without
`--select`) and **say so in the report**; and if the plan reports `unknown_ids`, name them.
Carry triage's `why` for each selected unit into the report, its `deprioritised` reasons into
the "not covered" section, and the plan's `refilled` list so the developer can see which units
were reviewed only because agents were left over.

When triage is not needed, run the same command with `--max-batches <craft budget>` and no
`--select`; the mapper's ordering decides and the report says "triage skipped: N batches fit
the budget of M".

If `--plan` was passed, print the plan and the agent arithmetic here and **stop**.

**ARCHITECT** — `model: "opus"`, when `signals.architect` is non-empty or `--architect`.

```
You are reviewing the SHAPE of a change that has not been merged yet: class and module
responsibilities, boundaries, abstractions, idiomatic form. Not bugs, not method-level craft.

Read these files in full first:
  - $PLUGIN/charter/base.md              (the grounding rules, output format and severity)
  - $PLUGIN/charter/stage_architect.md   (your remit and your bar)
  - $PLUGIN/charter/design_principles.md (the vocabulary you reason in)
  - $PLUGIN/charter/writing.md           (how to write each finding)
  - $PLUGIN/charter/<language>.md        (its ARCHITECT-stage section is yours; skip the SENIOR one)
  - $PLUGIN/charter/candidates.md        (report every candidate; volume is the reducer's job)
  - $RUN_DIR/briefs/context.txt       (the change context: files, units, callers, coupling, signals)

Repository: <repo toplevel>
The diff is at $RUN_DIR/review.diff; `git diff <scope_args>` reproduces it. Read the changed files
in full, and Grep for prior art before claiming an abstraction is missing or duplicated.

Two things are explicitly NOT yours and are covered by other passes running alongside you:
correctness bugs and cross-file behaviour (the integration pass), and method-level craft (the
function pass). Report class, module and boundary shape only.

This is a review. Do not edit, create, delete, stage, commit or stash anything.

Output a JSON array in base.md's format with "stage": "ARCHITECT" on every finding, and nothing else.
```

**INTEGRATION** — `model: "opus"`, when `signals.integration` is non-empty or `--integration`.

```
You are reviewing whether a change that has not been merged yet works AS A SYSTEM: contracts
under their callers, behaviour spread across files, transaction and failure paths, repetition,
and changed behaviour with no test.

Read these files in full first:
  - $PLUGIN/charter/integration.md       (your remit, your bar, your output format)
  - $PLUGIN/charter/writing.md           (how to write each finding)
  - <each file in the plan's `lenses` list, e.g. $PLUGIN/charter/lens_persistence.md>
  - $PLUGIN/charter/candidates.md        (report every candidate; volume is the reducer's job)
  - $RUN_DIR/briefs/context.txt       (every changed function, its callers, its tests, coupling, signals)

Repository: <repo toplevel>
<when the plan's `siblings` is non-empty, add: "This change spans repositories. The others are
<label> at <path>, ... . Read them — a call site written `<label>:<path>:<line>` lives there, and
that seam is the part of this change no other pass can see. Their diffs belong to their own runs,
so report what disagrees, not their internal quality.">
The diff is at $RUN_DIR/review.diff; `git diff <scope_args>` reproduces it. The call sites and test
references in the context are name-based leads: open them before you cite them.

Method-level craft and class shape are other passes' work. Report only what needs two or more
places to be true at once, or what is absent.

This is a review. Do not edit, create, delete, stage, commit or stash anything.

Output the JSON array specified in integration.md and nothing else.
```

**PO** — `model: "sonnet"` (only with `--po`, and only if a story was found). Read `base.md`,
`stage_po.md` and `writing.md`, plus the story text inline. It follows the stage's scope classification and
anchor rules and emits `"stage": "PO"`. Uncommitted work is usually mid-story, so lean hard on
the PARTIAL classification — an unfinished branch is not a missing acceptance criterion.

**When the plan's `siblings` is non-empty, this pass sees one repo's share of the story and
must be told so**, or it reports every criterion the other side implements as missing and the
scorecard is worse than no scorecard. Name the other repos in its prompt, tell it to judge
only the criteria this repo is responsible for, and to list the rest as "delivered
elsewhere in this change" rather than classifying them. The same caution applies to MAKER,
which designs the whole story and would read the missing half as a divergence: give it the
repo set and the boundary between them, or leave the pass off on a split change.

**MAKER** (only with `--maker` or `--deep`). Two agents, the second depending on the first.

**The requirement comes from the story, never from the diff.** That is the entire mechanism: a
requirement derived from the implementation carries the implementation's decomposition, so the
design step can only ever re-derive what was built. The steps below are ordered to make that
impossible, and step 0 is allowed to cancel the pass.

0. **Get a story, or skip the pass.** Take the issue key from the branch name; if there is none,
   ask for it with `AskUserQuestion` (one question, free-text). Then skip the pass, in one line
   saying why, when any of these holds:
   - no key was given, or the flags mean nobody can be asked (`--yes`, or a non-interactive run);
   - the story has neither a problem statement nor acceptance criteria — without a brief the only
     place to derive the problem from is the diff, which defeats the mechanism, and declining
     loudly beats running blind in name only.

1. **Build the brief** — you do this yourself, not an agent, and nothing from the diff may enter
   it. Fetch the story with the same Jira tool as Step 5.
   - *Keep:* description, context, acceptance criteria, out of scope, dependencies, open
     questions, and any non-functional budget (latency, cost, scale, security, deploy coupling).
   - *Strip:* Technical Notes, Approach, Design & Architecture Considerations, Testing Notes, and
     anything else naming a mechanism. Those are the groomer's solution; handing them over leaves
     the design step nothing to decide. **Keep constraints, drop prescriptions** — a constraint
     narrows the space from outside ("QA and UAT share one cluster"), a prescription names the
     mechanism ("cache-aside with `SET NX PX`"). Lift real constraints out of a stripped section
     and carry them across, because dropping them wholesale produces confident, wrong findings.
   - *Verify, never repeat, a claim about another service.* A groomed sentence asserting how a
     neighbouring service behaves reads as external fact and so survives the strip rule. Grep that
     service's repo for the entity, field, migration or client call at issue and carry the
     verified fact with the file that evidences it; where you cannot verify it, leave it out rather
     than hedge it, because a claim the brief labels uncertain still hardens into a rejected
     alternative. Read a linked ADR the same way and carry only what it decided — an ADR's scope is
     routinely narrower than the story citing it.
   - *Scope it.* Name at most three **forks the story leaves open** — places an implementer must
     choose, derived from the ACs and constraints rather than from the diff — and tell the design
     step to spend its depth there. This is what keeps the pass affordable without importing the
     implementation's decomposition.
   - Write the brief to `$RUN_DIR/briefs/blind-brief.txt`, and the stripped prescriptions to
     `$RUN_DIR/briefs/blind-withheld.txt` for step 3 only.

2. **Materialise a blind tree.** A worktree keeps the object database, so a curious agent reaches
   the branch with `git log`. `git archive` does not:
   ```bash
   BASE_TREE=$(mktemp -d /tmp/review-base-XXXXXX)
   git archive <base> | tar -x -C "$BASE_TREE"
   test ! -d "$BASE_TREE/.git" && echo "blind: no git history present"
   ```
   *Design* — `model: "fable"`, charter `blind_design.md` + `design_principles.md`, given
   `$BASE_TREE`, the brief and the open forks. It must not look at the main tree. Freeze the sketch
   when it returns (`chmod 444`) and record `shasum -a 256 | cut -c1-16` for the summary; if the
   hash changes before step 3 reads it, abort the pass.

3. **Compare** — `model: "opus"`, charter `blind_compare.md` + `writing.md`, given the frozen
   sketch, the withheld prescriptions and the real code. Emits `"stage": "MAKER"`,
   `"kind": "design"`.

Remove the tree in Step 9. It is a plain directory, so `rm -rf` is enough.

### Wave A — the craft pass (skip if `--no-craft`)

**The review unit and the agent invocation are not the same thing.** The mapper groups changed
code into **cards** — one coherent thing to judge, always inside one file: a new file read whole,
a test file judged as a suite, one class's changed methods, or a file's module-level regions.
Cards are sized for coherence alone. It then **packs** cards into invocations up to a read
budget, so a fourteen-line card shares an agent with five others instead of spending a whole one.

An invocation therefore may span files, and that says nothing about the cards belonging
together. The brief states this explicitly, and that instruction is what makes packing safe: the
register shift the old never-span-files rule protected against came from holding several files'
worth of deep reading, not from glancing at a few small independent targets.

**Do not tell the agent to read whole files.** Each card says what to read — the whole file when
it is mostly new, otherwise the listed spans plus surrounding context. The unconditional
whole-file read is what made this pass expensive: on a 68-file change it had 76 agents reading
22,297 lines to review 1,459 changed ones, because every batch of a file re-read all of it.

Model: **`opus`**, or `sonnet` under `--fast` / `--quick`. Launch at most **8 concurrently**.

```
You are reviewing one or more REVIEW CARDS from a change that has not been merged yet.

Read these files in full first:
  - $PLUGIN/charter/function_review.md   (your lens, your bar, your output format)
  - $PLUGIN/charter/craft_lens.md        (the house charter at function level)
  - $PLUGIN/charter/craft_questions.md   (how to read the code — the questions, not the catalogue)
  - $PLUGIN/charter/writing.md           (how to write each finding)
  - $PLUGIN/charter/<language>.md        (its SENIOR DEVELOPER-stage section is yours; skip the ARCHITECT one)
  - <each lens file the brief names, e.g. $PLUGIN/charter/lens_money.md>
  - $PLUGIN/charter/candidates.md        (report every candidate; volume is the reducer's job)
  - $RUN_DIR/briefs/batch-<N>.txt     (your brief: your cards, what to read for each, the
                                          functions, their spans and changed lines, tags, call
                                          sites, coupled functions, test coverage)
  - $RUN_DIR/briefs/context.txt       (the CHANGE MAP section only: the shape of the whole
                                          change, for orientation. Facts, not findings.)

Repository: <repo toplevel>

Do this, ONE CARD AT A TIME. Finish a card completely before you open the next one.
  1. Read what the card says to read. A card marked `whole file` is mostly new code, so read
     all of it; otherwise read its listed spans plus enough surrounding code to judge them.
     Do not read entire large files you were not asked to.
  2. Run `git diff <scope_args> -- <the card's file>` to see exactly what changed.
  3. Use Grep and Glob freely to check claims: whether a helper already exists, whether a
     caller relies on the current return shape, whether a sibling has the same duplication.
     The brief's call sites are leads, not facts. A claim you did not check does not go in the output.
  4. Review the card against function_review.md. For a `suite` card, the review is the suite:
     fixture structure, shared setup, naming consistency, duplication between cases and whether
     the tests communicate the behavioural contract — not each case in isolation. For a
     `new-file` card, judge the artifact as a whole. For `class` and `region` cards, take each
     listed function in turn and give the last the same attention as the first.

Your cards are INDEPENDENT. They were scheduled into the same agent because they fit a budget
together — that is all it means. Do not infer that two cards form one design unit, do not look
for a relationship between them, and do not report a finding that spans them. Cross-file
behaviour, whole-change shape and missing tests belong to other passes and are covered there.

This is a review. Do not edit, create, delete, stage, commit or stash anything.

Output the JSON array specified in function_review.md and nothing else. Every finding must
name which listed function it is about, via its `file` and `line`.
```

## STEP 7 — Reduce, with a script

Collect every agent's output and write each one to a file, verbatim, labelled by pass:

```bash
mkdir -p $RUN_DIR/findings
cat > $RUN_DIR/findings/craft-<N>.json <<'EOF'
<the agent's output, exactly as returned>
EOF
# likewise architect.json, integration.json, po.json, maker.json
```

Then run the reducer, once for the data and once for the rendered list:

```bash
python3 "$PLUGIN/scripts/merge_findings.py" --repo <repo toplevel> --diff $RUN_DIR/review.diff \
  --plan $RUN_DIR/plan.json [--posture brutal] \
  $RUN_DIR/findings/*.json > $RUN_DIR/merged.json
python3 "$PLUGIN/scripts/merge_findings.py" --repo <repo toplevel> --diff $RUN_DIR/review.diff \
  --plan $RUN_DIR/plan.json [--posture brutal] \
  --markdown $RUN_DIR/findings/*.json > $RUN_DIR/merged.txt
```

The script does, deterministically and with fixtures, what used to be six prose steps performed
by eye: it parses each output leniently, drops malformed findings, **drops any finding whose
quoted code is not in the cited file or the diff**, dedupes exact repeats, collapses one pattern
reported at many lines into one finding that lists every location, collapses two passes on one
locus to the higher-altitude framing, folds symptoms into the root that explains them, applies the
posture's caps, and demotes an over-long body. That locus collapse runs in every posture now:
three agents describing one coupling from three altitudes was noise at any volume, and it is the
reason a single finding used to arrive three times. It never decides whether a finding is *right*:
that judgement was tried as a model-run verification pass in the CLI and over-pruned exactly the
craft findings this review exists to produce. The only content-based drop is "the code you quoted
is not there", which is the class of finding developers most often push back on, and every drop is
reported with its reason.

Two of those steps are worth knowing about when you read the output. **Symptom absorption** folds
two or more lower-altitude findings into the design or Architect finding above them, scoped to the
enclosing function because `--plan` is passed — but a bug is never folded in; it stays its own
finding and gains one line pointing at its root. **The writing bar** demotes a body past 150 words
(180 for a design or Architect finding) one severity rank and marks one past 250 words
`inline: false`. The demotion and its word count are shown on the card and in the markdown list,
so a badge that looks quieter than its finding says why. Nothing is dropped for length.

**Do not edit the reducer's output.** If it dropped something you think was real, say so in the
report as your own observation; do not put it back.

Then render the page — the diff with every finding under its line, filters, editor deep links,
and a valid / invalid / duplicate label on each card — and open it:

```bash
BRANCH_SLUG=$(git branch --show-current | tr '/' '-')
REPO_SLUG=$(basename "$(git rev-parse --show-toplevel)")   # one branch name spans every repo of
                                                           # a cross-repo change, so the branch
                                                           # alone does not name the report
python3 "$PLUGIN/scripts/render_review.py" --repo <repo toplevel> --diff $RUN_DIR/review.diff \
  --merged $RUN_DIR/merged.json --plan $RUN_DIR/plan.json \
  --title "<branch> · review" --scope "<scope label>" \
  --out "$RUN_DIR/review-${REPO_SLUG}-${BRANCH_SLUG}.html" \
  --sarif "$RUN_DIR/review-${REPO_SLUG}-${BRANCH_SLUG}.sarif" \
  --intellij "$RUN_DIR/review-${REPO_SLUG}-${BRANCH_SLUG}.xml"
open "$RUN_DIR/review-${REPO_SLUG}-${BRANCH_SLUG}.html" 2>/dev/null \
  || xdg-open "$RUN_DIR/review-${REPO_SLUG}-${BRANCH_SLUG}.html" 2>/dev/null || true
```

The page is a **local file, on purpose**: it holds the repository's diff and quoted code, so it
never leaves the machine unless the developer chooses to send it. Labels the developer clicks are
kept in the browser per review title and exported with the page's button; those labels are the
precision data the benchmark needs, so mention the export in the report. The SARIF file opens in
VS Code's SARIF Viewer extension and the XML imports into IntelliJ via *Code › Analyze Code › View
Offline Inspection Results*, for anyone who prefers the IDE's problems view.

## STEP 8 — Report

Print, in this order:

- **Orientation**: the scope label from Step 3, the branch, which passes ran and — for any that
  did not — the one-line reason (`architect skipped: single production file, no structural
  signals`, `triage skipped: 5 batches fit the budget of 9`), the posture, the model that ran the
  craft pass, how many agents you spent, the reducer's counts line from `$RUN_DIR/merged.txt`,
  and the path of the rendered page, so the developer can reopen it.
- **The top three findings in prose.** This is the part that gets read. Explain each in plain
  language: what it is, where, why it will cost someone, what to do. Choose from the reducer's
  ranked list; do not introduce anything that is not in it.
- **The full list**, exactly as `$RUN_DIR/merged.txt` renders it: grouped by file, each
  finding with its severity, location, enclosing function, pass and body, followed by the
  reducer's own drops with reasons.
- **What was not covered**, named individually:
  - every batch in the plan's `skipped` list, with its file and whether it was production or
    test, plus the `--max-agents` value that excluded it;
  - every unit in the plan's `trivial` list, with the mapper's reason, and the flag that would
    review them;
  - passes switched off by a flag or by empty signals, the PO pass when no story was found, and
    `signals.maker` when it fired and MAKER did not run, and the reason MAKER cancelled itself
    when it did — no issue key, or a story with no problem statement and no acceptance criteria;
  - triage's `deprioritised` reasons and the plan's `refilled` ids, when triage ran;
  - every warning from the plan's `warnings` array, verbatim in substance;
  - files the mapper classified as non-code;
  - anything in `git status` outside the scope you reviewed — untracked files are the common
    one, since `git diff` cannot see them until they are staged.

That last section is not optional. An uncapped review that quietly skipped half the change is
more misleading than a capped one that says so.

Then stop. Do not offer to fix the findings unless the user asks — they asked for a review, and
a reviewer who starts editing has stopped being a reviewer. If they do ask, treat it as a fresh
request and work through the findings in severity order.

## STEP 9 — Clean up

```bash
rm -rf "$BASE_TREE"                                    # only if MAKER ran
rm -rf "$RUN_DIR/briefs" "$RUN_DIR/findings"           # scratch; the merged output stays
git worktree list                                      # confirm nothing is left behind
```

Leave `$RUN_DIR` itself, and say where it is. The rendered page, the merged JSON and the diff
are what the developer opens after the run, and a concurrent second review no longer has any
reason to remove them. The operating system clears the directory in its own time.

---

## RULES

- **Set `model` on every Agent call.** The table in Step 6 is the assignment; do not fall back
  to the default.
- **Every pass is a fresh subagent; never a `fork`.** The calling session knows why the code is
  the way it is, and that knowledge is disqualifying for a reviewer. Keep the review in clean
  contexts and keep yourself to plumbing: findings come only from agents.
- **Launch everything that does not depend on triage in Wave 0, together.** The wide passes
  never wait for the craft wave.
- **The scripts own the facts, the merge and the page.** Spans, callers, tags, coupling,
  triviality and batching come from `changed_functions.py`; dedup, quote checks, pattern collapse
  and caps come from `merge_findings.py`; the rendered page comes from `render_review.py`. Do none
  of it by hand, and do not override any script's output.
- **Everything this run writes goes under `$RUN_DIR`**, a `mktemp -d` directory made in Step 2.
  Never a fixed path: two runs of this skill overlap routinely — one per repository of a
  cross-repo change — and a fixed path makes the second destroy the first's briefs while its
  agents are still reading them.
- **The page stays local.** It is written under `$RUN_DIR` and opened from there. Do not publish it
  anywhere — not as an artifact, not to a share — unless the developer explicitly asks in this
  session, because it contains the repository's diff and code.
- **Never post anywhere.** Not to GitLab, not to Jira, not to Slack. This review is for the
  person who asked for it, in this session.
- **Never modify the code under review.** No edits, no staging, no commits, no stashing, no
  formatters — by you or by any agent you launch. The user has uncommitted work in this tree,
  and a review that touches it can destroy work the user cannot get back.
- **Work in place; only MAKER gets its own tree**, a `git archive` extract of the pre-change
  commit with no `.git` in it, removed afterwards.
- **Quote or drop, in every mode.** Brutal mode lifts the volume ceiling and nothing else, and
  the reducer checks the quotes.
- **Report the scope you reviewed**, in words, every time, and **say what you skipped** — every
  cap hit, every pass the signals turned off, every mechanical edit set aside, every ignored file.
- **The charter is the source of truth.** Do not review from memory of these rules, and do not
  edit the charter to change one review — it is shared with the `mr-reviewer` CLI, so an edit
  changes what the bot posts on every MR it touches.
