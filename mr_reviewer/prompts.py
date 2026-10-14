"""
Language-specific review prompts for code analysis.

The prompt bodies — the review charter — live in the mr-review plugin under
``plugins/mr-review/charter/`` rather than inside this package. There is exactly one copy and
two consumers read it: this CLI, and the ``/review-mr`` skill the plugin ships. A synced second
copy was the alternative and it drifts the moment someone edits whichever one they happen to be
looking at, which is the failure this repo has already had with its README.
"""

import os
from functools import lru_cache
from pathlib import Path

from .config import jira_mcp_tool

_PACKAGE_DIR = Path(__file__).parent


# One override that moves the whole plugin, read by every subdirectory lookup below. The charter
# and the reduce step are a matched pair — `candidates.md` tells each pass to emit candidates
# *because* the reducer applies the posture afterwards, and the reducer's stage table has to know
# the stage names the charter asks agents to emit — so pointing one at an older checkout while the
# other stays put pairs a charter with a reducer that never shipped together, and nothing notices.
_PLUGIN_ENV = "MR_REVIEWER_PLUGIN"


def plugin_subdir(name: str, sentinel: str, legacy: Path, env_var: str) -> Path:
    """Locate one directory inside the mr-review plugin, preferring the plugin that owns it.

    Candidates are ordered by specificity: the directory's own override, then that directory
    inside `$MR_REVIEWER_PLUGIN`, then the plugin beside this package (the normal case — the CLI
    runs from its checkout, and a built wheel puts it at the same relative path), then the
    pre-move location so an older install degrades to a warning-free success rather than an
    import error. ``sentinel`` is the file whose presence proves the directory is the real one.

    Shared by `charter_dir` and `reduce.scripts_dir`, which were eighteen near-identical lines
    apiece. The *order* of this chain is the load-bearing part, so it exists once.
    """
    plugin = os.environ.get(_PLUGIN_ENV)
    override = os.environ.get(env_var)
    candidates = (
        ([Path(override)] if override else [])
        + ([Path(plugin) / name] if plugin else [])
        + [_PACKAGE_DIR.parent / "plugins" / "mr-review" / name, legacy]
    )
    for candidate in candidates:
        if (candidate / sentinel).is_file():
            return candidate
    raise FileNotFoundError(
        f"mr-review {name} not found — looked in "
        + ", ".join(str(c) for c in candidates)
        + f". Set {env_var} to the directory holding {sentinel}, or {_PLUGIN_ENV} to the plugin."
    )


@lru_cache(maxsize=1)
def charter_dir() -> Path:
    """Locate the review charter, preferring the plugin that owns it."""
    return plugin_subdir("charter", "base.md", _PACKAGE_DIR / "prompts", "MR_REVIEWER_CHARTER")


def brutal_mode() -> bool:
    """Whether this run lifts the volume caps (``--brutal`` / ``MR_REVIEWER_BRUTAL=1``).

    Read from the environment rather than threaded through every signature, matching how the blind
    design pass and learning mode already work.

    It now has exactly one consumer — `reduce.posture_for`, which turns it into the `brutal`
    posture — where it used to have three, each enforcing the mode for itself because a caller
    that forgot the flag silently re-imposed a cap. That defence is no longer needed: there is one
    place volume is decided, so forgetting it is not possible. The flag changes no prompt, because
    every pass already reviews at full depth and emits candidates.
    """
    return bool(os.environ.get("MR_REVIEWER_BRUTAL"))


def _candidates_suffix() -> str:
    """The candidates amendment, appended after the volume rules it overrides.

    Appended on **every** run, not just under ``--brutal``. Detection and presentation are
    separate: each pass reviews at full depth and emits candidates, and the run's posture is
    applied afterwards by the reduce step, which can only ever choose from what was emitted. A
    quieter reviewer loses the subtle findings irrecoverably; a quieter report loses nothing,
    because every suppressed finding is in the merged output with the cap that removed it.

    The two rules that are *not* volume — one framing per locus, symptoms under their root — stay
    in force inside it, because the reducer collapses and folds in every posture.
    """
    return "\n\n---\n\n" + _load_prompt("candidates")


def build_jira_section(jira_tickets: list[str] | None, jira_cloud_id: str) -> str:
    """Build the JIRA STORY CONTEXT section, or empty string if no tickets / cloud id."""
    if not (jira_tickets and jira_cloud_id):
        return ""
    ticket_list = ", ".join(jira_tickets)
    fetch_instructions = "\n".join(
        f"- Fetch **{ticket}** using the `{jira_mcp_tool()}` tool "
        f'with `cloudId: "{jira_cloud_id}"`, `issueIdOrKey: "{ticket}"`, '
        f'`fields: ["summary", "description", "status", "issuetype"]`, '
        f'`responseContentFormat: "markdown"`'
        for ticket in jira_tickets
    )
    return f"""
---

## JIRA STORY CONTEXT

The following Jira ticket(s) are referenced in this MR: **{ticket_list}**

**BEFORE reviewing the code**, fetch the story details so you understand the requirements:
{fetch_instructions}

Use the story description and acceptance criteria to:
- Verify the implementation matches what the story asks for
- Flag any acceptance criteria that appear unaddressed by the changes
- Flag implementation that goes beyond or contradicts the story scope

If the tool call fails, continue the review without Jira context — do NOT let a failed fetch block the review.
"""


@lru_cache(maxsize=10)
def _load_prompt(name: str) -> str:
    """
    Load a prompt from a markdown file.

    Args:
        name: Name of the prompt file (without .md extension)

    Returns:
        Prompt content as string
    """
    prompt_file = charter_dir() / f"{name}.md"

    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

    return prompt_file.read_text(encoding="utf-8")


_STAGES_MARKER = "<!-- STAGES -->\n"
_STAGE_FILES = ("stage_po", "stage_architect", "stage_senior")


def get_base_prompt() -> str:
    """Get the base review prompt (multi-stage: PO → Architect → Senior) plus the clean-code charter.

    The three stage bodies live in their own files and are spliced into ``base.md`` at its marker.
    That split exists for the skill, whose ARCHITECT and PO agents read only their own stage
    instead of the whole rubric; the CLI still gets the identical assembled prompt, and there is
    still exactly one copy of every stage's text.

    The craft questions follow the clean-code charter, because the charter is a catalogue and the
    questions are how the Senior Developer stage finds what is in it. A reviewer reading a batch of
    files against a catalogue reports what is salient and walks past the judgement calls, which is
    the failure this stage is most prone to; the skill's per-function agent reads the same file.

    The writing-style charter comes last of the three: it governs how every finding's
    title and body are written, for the bot's posted comments as much as for the skill's page.

    The candidates amendment is appended *last*, so its suspension of the stage budgets is read
    after the budgets it suspends. It is appended on every run: how much gets reported is the
    reduce step's decision, made with every pass's output in view, and it cannot recover a finding
    a pass decided not to mention.
    """
    stages = "---\n\n".join(_load_prompt(name) for name in _STAGE_FILES)
    base = _load_prompt("base").replace(_STAGES_MARKER, stages)
    return (
        base
        + "\n\n"
        + _load_prompt("clean_code")
        + "\n\n---\n\n"
        + _load_prompt("craft_questions")
        + "\n\n---\n\n"
        + _load_prompt("writing")
        + _candidates_suffix()
    )


def get_language_prompt(language: str) -> str:
    """
    Get language-specific prompt guidance.

    Args:
        language: Programming language (java, angular, typescript, generic)

    Returns:
        Language-specific prompt content
    """
    # Map language aliases
    language_map = {
        "java": "java",
        "angular": "angular",
        "typescript": "angular",  # Plain TypeScript uses Angular prompt
        "react": "react",
        "jsx": "react",
        "tsx": "react",
        "python": "python",
        "py": "python",
        "generic": "generic",
    }

    prompt_name = language_map.get(language.lower(), "generic")
    return _load_prompt(prompt_name)


def _prompt_review_guidance(prompt_guide_content: str | None = None) -> str:
    """Build the prompt-file review guidance block, optionally embedding the repo's guide inline."""
    block = _load_prompt("prompt_review")
    if prompt_guide_content:
        block += (
            "\n\n### Repo's prompt-engineering guide (embedded)\n\n"
            "Apply these rules as the authoritative charter for prompt-file review in this MR:\n\n"
            f"{prompt_guide_content}\n"
        )
    return block


def get_review_prompt(
    language: str,
    diff_content: str,
    mr_title: str,
    mr_description: str,
    files_context: dict[str, str] | None = None,
    related_files: dict[str, str] | None = None,
    jira_tickets: list[str] | None = None,
    jira_cloud_id: str = "",
    has_markdown_files: bool = False,
    prompt_guide_content: str | None = None,
) -> str:
    """
    Build the complete review prompt for a given language.

    Args:
        language: Programming language (java, angular, or generic)
        diff_content: The full diff content to review
        mr_title: Title of the merge request
        mr_description: Description of the merge request
        files_context: Optional dict mapping file paths to their full content
        related_files: Optional dict of related files (entities, interfaces) for context
        jira_tickets: Optional list of Jira ticket IDs found in MR title/description
        jira_cloud_id: Atlassian cloud ID for MCP tool calls

    Returns:
        Complete prompt string for Claude
    """
    base_prompt = get_base_prompt()
    language_guidance = get_language_prompt(language)
    if has_markdown_files:
        language_guidance = (
            language_guidance + "\n\n" + _prompt_review_guidance(prompt_guide_content)
        )

    # Build file context section if provided
    context_section = ""
    if files_context:
        context_parts = []
        for file_path, content in files_context.items():
            context_parts.append(f"### {file_path}\n\n```\n{content}\n```")
        context_section = f"""
---

## FULL FILE CONTEXT (Review Target - Not Just Reference)

Review the ENTIRE method and class for structural quality, modularity, and design -- not just the changed lines. Assess: method length, class size, SRP, whether behavior belongs in domain entities.

The diff below shows what changed. These full files show the complete picture. Use them to evaluate the health of the code AROUND the changes.

{chr(10).join(context_parts)}
"""

    # Build related files section (entities, interfaces, etc.)
    related_section = ""
    if related_files:
        related_parts = []
        for file_path, content in related_files.items():
            related_parts.append(f"### {file_path}\n\n```\n{content}\n```")
        related_section = f"""
---

## RELATED FILES (Domain Entities, Interfaces)

These files are NOT being modified but are referenced by the changed code. Use them to understand:
- Domain model structure and behavior (for detecting Anemic Domain Model)
- Interface contracts that implementations must fulfill
- Entity relationships and constraints

{chr(10).join(related_parts)}
"""

    jira_section = build_jira_section(jira_tickets, jira_cloud_id)

    return f"""{base_prompt}

{language_guidance}

---

## MERGE REQUEST CONTEXT

**Title:** {mr_title}

**Description:** {mr_description or "No description provided"}
{jira_section}{context_section}{related_section}
---

## DIFF TO REVIEW

```diff
{diff_content}
```

---

Remember: Output ONLY a valid JSON array of comments. No additional text before or after the JSON.
"""


# Focused single-stage pass labels for team/orchestrated reviews.
_FOCUS_LABELS = {
    "PO": "PRODUCT OWNER — Acceptance-Criteria coverage",
    "ARCHITECT": "ARCHITECT — system/class shape, domain modelling, idiomatic use, abstractions",
    "SENIOR": "SENIOR DEVELOPER — function-level craft (will this be maintainable in 3 months?) + real correctness bugs",
    "PROMPT": "PROMPT REVIEW — quality of changed LLM prompt files (clarity, instructions, examples, structure), NOT code craft",
}


def _focus_block(focus_stage: str, focus_note: str = "") -> str:
    """Directive that scopes a review call to ONE stage (for the orchestrated team).

    Reuses the full multi-stage rubric below for context but tells the model to emit
    findings for a single stage only, so each persona gets its own focused call.
    """
    label = _FOCUS_LABELS.get(focus_stage, focus_stage)
    note = f"\n{focus_note}\n" if focus_note else ""
    return f"""## ⚙️ FOCUSED SINGLE-STAGE PASS — {label}

This review is split across several independent passes, each run by a separate reviewer and merged afterwards. **For THIS call you run ONLY the {focus_stage} stage.**

- Emit findings for the **{focus_stage} stage ONLY**. Every comment you output MUST have `"stage": "{focus_stage}"`.
- Do NOT emit findings for the other stages — separate passes own those. Ignore the "run THREE passes in order" framing in the rubric below; for THIS call you run exactly one stage.
- Apply that stage's scope, budget, bar, and (for SENIOR) the `kind` field exactly as the rubric below describes.
{note}
---

"""


def get_local_review_prompt(
    language: str,
    diff_content: str,
    mr_title: str,
    mr_description: str,
    jira_tickets: list[str] | None = None,
    jira_cloud_id: str = "",
    has_markdown_files: bool = False,
    focus_stage: str | None = None,
    focus_note: str = "",
) -> str:
    """
    Build the prompt for local-clone-mode reviews where Claude has Read/Glob/Grep
    access to the cloned repo. Context files are NOT embedded — Claude reads them via tools.

    When ``focus_stage`` is set ("PO" | "ARCHITECT" | "SENIOR"), the prompt is scoped to
    that single stage — used by the orchestrated review team so each persona runs in its
    own focused ``claude -p`` call. ``focus_note`` adds per-pass guidance (e.g. "this is a
    batch of N files" for SENIOR, or "you have the whole-MR map" for ARCHITECT).
    """
    base_prompt = get_base_prompt()
    language_guidance = get_language_prompt(language)
    if has_markdown_files:
        # In local mode Claude reads docs/prompt-engineering.md itself via Read tool.
        language_guidance = language_guidance + "\n\n" + _prompt_review_guidance(None)
    jira_section = build_jira_section(jira_tickets, jira_cloud_id)
    focus_prefix = _focus_block(focus_stage, focus_note) if focus_stage else ""
    stage_reminder = f' Every comment MUST have "stage": "{focus_stage}".' if focus_stage else ""

    return f"""{focus_prefix}{base_prompt}

{language_guidance}

---

## MERGE REQUEST CONTEXT

**Title:** {mr_title}

**Description:** {mr_description or "No description provided"}
{jira_section}
---

## INSTRUCTIONS: You Have Full Repo Access

You are reviewing this MR from a checked-out copy of the repository at the MR branch.
You have access to Read, Glob, and Grep tools to browse the full codebase.

**IMPORTANT — investigate efficiently, don't slurp whole files.** Do NOT review from the diff
hunk alone (it usually truncates the method). But you also don't need to read entire large files.
Read what you need to judge the change:
- **Read the full method/class each change touches** — so you can assess method length, modularity,
  SRP, and whether behaviour belongs in a domain entity. (The hunk is not enough; the whole 2000-line
  file is too much.)
- **Grep** for related definitions, callers/usages, the types involved, and — importantly — whether a
  helper/constant/abstraction already exists (prior-art / duplication checks).
- Open related classes (entities, interfaces, DTOs) only when a finding depends on them.

Use Read for targeted regions and Grep/Glob to navigate — reach for the codebase as the finding
requires, not by default.

**Before you cite any file, test, class, or symbol in a comment, OPEN it with your tools and
confirm it exists and says what you claim.** Do not name a test (e.g. `TestFoo`) or assert a
value/key/AC unless you have read it here. If you cannot verify a claim from this repo, drop it
or ask — do not assert it. (See the "Investigate before you claim — quote, or drop" rule above.)

---

## DIFF TO REVIEW (Use as a Map of What Changed)

The diff shows what was modified. Use it as a guide for WHAT to review, then Read the
full files to understand the complete context.

```diff
{diff_content}
```

---

Remember: Output ONLY a valid JSON array of comments. No additional text before or after the JSON.{stage_reminder}
"""


def get_audit_prompt(
    language: str,
    repo_name: str,
    branch: str,
    scope: str = "",
) -> str:
    """
    Build a whole-repo audit prompt.

    Combines the audit framing, dominant-language guidance, and the clean-code
    charter so the auditor has the same structural lens used for MR reviews
    but reframed for an unbounded codebase.

    Args:
        language: Dominant language (java, angular, react, python, generic)
        repo_name: Display name of the repo (for the report header)
        branch: Branch being audited (for the report header)
        scope: Optional subtree path to restrict the audit to (relative to repo root)

    Returns:
        Complete prompt string for the auditor
    """
    audit_framing = _load_prompt("audit")
    language_guidance = get_language_prompt(language)
    charter = _load_prompt("clean_code")

    scope_block = ""
    if scope:
        scope_block = f"""
---

## AUDIT SCOPE

Restrict your audit to the subtree `{scope}` (relative to the repo root). Use `Glob` with patterns rooted at that path. Do not produce findings about code outside this subtree, though you may read outside it if needed to understand a dependency.
"""

    return f"""{audit_framing}

---

## LANGUAGE / FRAMEWORK GUIDANCE

The dominant language in this repo is **{language}**. The patterns below were written for diff-review but apply equally to whole-codebase audit — treat each item as a hint about what to look for at the repo level. **Ignore any lint-tier items mentioned in this guidance** — the audit prompt above is authoritative on what NOT to flag.

{language_guidance}

---

## CLEAN-CODE CHARTER

These are the shared principles used across all review and audit work in this project. Apply them through the structural lens described above.

{charter}
{scope_block}
---

## REPO METADATA (for the report header)

- **Repo name:** {repo_name}
- **Branch:** {branch}

Begin the audit now. Use Read, Glob, and Grep to orient, then produce the markdown report described in the OUTPUT FORMAT section. Output ONLY the markdown — no preamble, no code fences around the whole document.
"""


def get_context_request_prompt(
    diff_content: str,
    mr_title: str,
    mr_description: str,
) -> str:
    """
    Build a prompt to ask Claude what context files it needs for review.

    This is the first pass of the two-pass approach to reduce token usage.

    Args:
        diff_content: The diff content to analyze
        mr_title: Title of the merge request
        mr_description: Description of the merge request

    Returns:
        Prompt string for requesting context needs
    """
    template = _load_prompt("context_request")
    return template.format(
        mr_title=mr_title,
        mr_description=mr_description or "No description provided",
        diff_content=diff_content,
    )


def get_verification_prompt(diff_content: str, comments: list[dict]) -> str:
    """Build a prompt that asks Claude to filter weak findings out of a proposed comment list."""
    import json as _json

    template = _load_prompt("verification")
    comments_json = _json.dumps(comments, indent=2)
    return template.format(diff_content=diff_content, comments_json=comments_json)


def get_summary_prompt(comments: list[dict], mr_title: str) -> str:
    """
    Build a prompt to generate a summary comment for the MR.

    Args:
        comments: List of review comments
        mr_title: Title of the merge request

    Returns:
        Prompt for generating summary
    """
    critical_count = sum(1 for c in comments if c.get("severity") == "CRITICAL")
    warning_count = sum(1 for c in comments if c.get("severity") == "WARNING")
    suggestion_count = sum(1 for c in comments if c.get("severity") == "SUGGESTION")

    issues_list = "\n".join(f"- [{c.get('severity')}] {c.get('title')}" for c in comments)

    # Load and format the summary template
    template = _load_prompt("summary")

    return template.format(
        mr_title=mr_title,
        critical_count=critical_count,
        warning_count=warning_count,
        suggestion_count=suggestion_count,
        issues_list=issues_list if issues_list else "No issues found",
    )


def _blind_brief_rules(jira_tickets: list[str] | None, jira_cloud_id: str) -> str:
    """Tell the design agent how to turn the story into a brief, and what to refuse to read.

    The skill builds this brief in its orchestrator and hands over finished text. The CLI has no
    Jira access of its own, so the design agent fetches the story itself — which means the strip
    rules have to travel with the prompt and be applied by the same agent that will design from
    them. Weaker than building it upstream, and still far better than a requirement derived from
    the diff, which carries the implementation's own decomposition.
    """
    tickets = ", ".join(jira_tickets or [])
    fetches = "\n".join(
        f'- **{t}** — `{jira_mcp_tool()}` with `cloudId: "{jira_cloud_id}"`, '
        f'`issueIdOrKey: "{t}"`, `fields: ["summary", "description", "status", "issuetype"]`, '
        f'`responseContentFormat: "markdown"`'
        for t in (jira_tickets or [])
    )
    return f"""
---

## YOUR REQUIREMENT — build it from the story, first

Fetch the story before you read a single source file:
{fetches}

Then turn it into your brief, and be strict about what crosses over:

- **Keep** the description, context, acceptance criteria, out of scope, dependencies, open
  questions, and any non-functional budget — latency, cost, scale, security, deploy coupling.
- **Strip** Technical Notes, Approach, Design & Architecture Considerations and Testing Notes.
  Those are the groomer's solution; designing from them leaves you nothing to decide. **Keep
  constraints, drop prescriptions** — a constraint narrows the space from outside ("QA and UAT
  share one cluster"), a prescription names the mechanism ("cache-aside with `SET NX PX`"). Lift
  genuine constraints out of a stripped section and carry them across, because dropping them
  wholesale makes you argue confidently for the unworkable.
- **Do not trust a claim about another service.** A groomed sentence asserting how a neighbouring
  service behaves reads as external fact and survives the strip rule, and it is as likely to be
  the reason this change exists as a record of anything anyone decided. You cannot reach that
  repository from here, so treat every such claim as an assumption and record it in *Assumptions
  and unknowns* — never as a constraint you designed around.
- A linked ADR is worth reading, and carries only what it actually decided. Its scope is routinely
  narrower than the story citing it.
- Note which decisions the **acceptance criteria already fix**. Where an AC mandates the
  mechanism you were never free, and the comparison must neither credit nor fault the
  implementation for it.
- Name at most **three forks the story leaves open** — places an implementer must choose, derived
  from the ACs and constraints — and spend your depth there rather than spreading it evenly.

If the story turns out to have neither a problem statement nor acceptance criteria, say exactly
that in one line and stop. Without a brief the only place left to derive the problem from is the
implementation, which defeats this pass entirely, and declining is the correct outcome.

**You will not be shown the diff, the merge-request title or description, the branch name or the
commit messages.** Referenced tickets: {tickets}.
"""


def get_blind_design_prompt(
    language: str,
    jira_tickets: list[str] | None = None,
    jira_cloud_id: str = "",
) -> str:
    """Build the blind-design prompt: design the change from the story, before seeing it.

    Receives the *requirement* only. The MR title and description are withheld because both
    narrate the chosen implementation, and the diff is withheld for the same reason; the pass runs
    in a `git archive` extract of the pre-change commit, so the isolation is physical rather than
    an instruction the model has to honour.
    """
    return f"""{_load_prompt("blind_design")}

{_load_prompt("design_principles")}

{get_language_prompt(language)}
{_blind_brief_rules(jira_tickets, jira_cloud_id)}
---

## YOUR WORKING TREE

You have Read, Glob and Grep over a copy of this repository **at the commit before this change was
made**, with no git history in it. Files the change adds are absent; files it modifies are in their
pre-change form. Use the tools to find prior art, existing conventions, real persisted field names,
and what the platform already provides — then design.

Output the markdown sketch only.
"""


def get_blind_compare_prompt(
    language: str,
    diff_content: str,
    mr_title: str,
    mr_description: str,
    design_sketch: str,
) -> str:
    """Build the MAKER phase-2 prompt: compare the phase-1 sketch against the real implementation.

    Carries the design charter, because a pass with no principles vocabulary can only report the cost
    shapes its prompt happens to enumerate. What keeps it out of ARCHITECT and SENIOR territory is the
    *remit* rule — it reports only where the implementation diverges from its own design, and only on
    the choice of mechanism and the placement of work and types — not a shortage of principles. It
    still excludes the multi-stage rubric, which would reintroduce the other stages' scopes wholesale.
    """
    return f"""{_load_prompt("blind_compare")}

{_load_prompt("design_principles")}

---

## THE DESIGN SKETCH (written from the story, before the implementation could be seen)

{design_sketch}

---

## MERGE REQUEST CONTEXT

**Title:** {mr_title}

**Description:** {mr_description or "No description provided"}

---

## WHAT WAS ACTUALLY BUILT

You now have Read, Glob and Grep over the repository at the MR branch, so the implementation is
available in full. The diff below is a map of what changed — open the files it names to judge a
decision, and grep for the callers and downstream methods a leaked contract would touch, since
that is where the cost of a decision usually shows up.

```diff
{diff_content}
```

---

Remember: diff decisions, not lines. One comment per root decision, each with a named cost.{_candidates_suffix()}
Output ONLY a valid JSON array. Every comment MUST have "stage": "MAKER" and "kind": "design".
"""


def _learning_concepts_block(concepts: list[str] | None) -> str:
    """Render the concept names already used in the inline addenda, for naming consistency."""
    if not concepts:
        return ""
    return (
        "\n\nConcepts named in the inline addenda: " + ", ".join(f"**{c}**" for c in concepts) + "."
    )


def get_learning_prompt(
    language: str,
    mr_title: str,
    findings_block: str,
    diff_content: str,
    concepts: list[str] | None = None,
) -> str:
    """Build the learning-mode prompt: turn a finished review into a study document.

    Runs once, after the review has merged, over the findings it produced. Structured as a
    post-processing stage rather than another review pass for two reasons: the finding work is already
    done and re-doing it would double the cost, and a pass that both hunts and teaches tends to do
    neither well.

    Carries the design charter because the whole point is to teach the named concept and the reasoning
    behind it, not to restate the finding in gentler words.
    """
    return f"""{_load_prompt("learning")}

{_load_prompt("design_principles")}

{get_language_prompt(language)}

---

## THE MERGE REQUEST

**Title:** {mr_title}

---

## WHAT THE REVIEW FOUND

These are the merged findings, before any volume caps were applied — so several may share one
concept. Grouping them is your job, and where a concept recurs, the recurrence is the lesson.

The findings already carry inline teaching addenda, added in a previous step and visible below. Use
the same concept names so the document and the MR comments agree — a reader moving between them should
not have to work out that two names mean the same idea. Do not simply concatenate the addenda: the
document's job is the grouping, the ranking, and the reading list that a single comment cannot give.
{_learning_concepts_block(concepts)}

{findings_block}

---

## THE DIFF

You also have Read, Glob and Grep over the repository at the MR branch. Use them to quote code
accurately, to confirm every file and line you cite, and to find an existing example in this codebase
of each concept done well.

```diff
{diff_content}
```

---

Write the study document now. Markdown only.
"""


def get_learning_comment_prompt(
    language: str,
    mr_title: str,
    findings_block: str,
) -> str:
    """Build the learning-mode comment-enrichment prompt: teach the concept behind each finding.

    Additive by design — it returns addenda keyed by (file, line) that get appended to existing comment
    bodies, rather than rewriting them. A rewrite could damage a finding that the review already got
    right; an append cannot. It is also why this can run on the merged output instead of being threaded
    into all five review prompts, where it would compete with the finding work for attention.

    No diff and no repo access: the addendum must not introduce claims the finding did not establish,
    so withholding the sources of new claims is the cheapest way to enforce that.
    """
    return f"""{_load_prompt("learning_comment")}

{_load_prompt("design_principles")}

{get_language_prompt(language)}

---

## THE MERGE REQUEST

**Title:** {mr_title}

---

## THE FINDINGS TO ANNOTATE

{findings_block}

---

Output ONLY the JSON array of addenda. Skip any finding whose concept is not worth teaching.
"""
