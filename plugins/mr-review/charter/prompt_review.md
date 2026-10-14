## PROMPT-FILE REVIEW (additional guidance)

One or more `.md` files were changed in this MR. **Some may be LLM prompts; most are probably ordinary documentation.** This section applies only to real prompt files.

### Step 0 — Decide per file whether it is actually a prompt

For each changed `.md` (or `.markdown`) file, read the content and classify it:

**Signals it IS a prompt** (apply this section):
- Imperative second-person opening in the first ~20 lines: `You are...`, `Your task is...`, `Your role is...`, `Your job...`
- Direct model directives: `You MUST`, `You will`, `Do NOT`, `Output ONLY`, `Return a JSON`
- Prompt-management frontmatter keys: `model:`, `temperature:`, `version:`, `tags:`, `name:` (Langfuse / Promptfoo / Pezzo conventions)
- `{{variable}}` / `{variable}` template placeholders used as runtime substitution points
- Reads as instructions *to a model*, not as prose for a human

**Signals it is documentation** (skip this section for that file):
- Third-person describing voice: "This guide explains...", "We use prompt X to do Y..."
- Table of contents, `## Overview`, `## How it works` sections
- Talks *about* prompts, agents, or LLM systems rather than *being* one
- Located at `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, or anywhere under `docs/`
- A guide *about* prompt engineering (including the very `docs/prompt-engineering.md` mentioned below — that is a reference doc, not a prompt)

**If you cannot tell, lean toward documentation** — do not invent prompt-review findings for a file that is plain prose. Documentation gets reviewed under the rest of the charter (clarity, accuracy, no contradictions) without the prompt-specific checks.

The rest of this section only applies to files you classified as real prompts. If no changed `.md` file is a prompt, skip Section entirely.

---

Review prompt files as you would code — they are production artefacts, just authored in natural language. The same senior-reviewer posture applies: catch design and intent issues, skip nits.

### Before reviewing prompt files, load the repo's best-practices guide

The repo may have a prompt-engineering best-practices guide at **`docs/prompt-engineering.md`** (relative to the repo root). If you have file-access tools available (Read / Glob / Grep):

1. Read `docs/prompt-engineering.md`. If it exists, **treat its rules as the authoritative review charter for prompt files in this MR.** The guidance below is a fallback for when that file is missing or thin.
2. If `docs/prompt-engineering.md` is not present, search for similarly-named files (`prompt-guide.md`, `llm-best-practices.md`, `prompts/README.md`) before falling back to the generic guidance below.
3. If no guide exists in the repo, apply the generic guidance below and add a single SUGGESTION-severity note recommending that the team commit one — without spelling out its contents.

### Generic prompt-review guidance (fallback when the repo guide is missing)

Treat prompts with the same rigour as code. A prompt is a contract with the model; ambiguity in a prompt is ambiguity in production behaviour.

**Structure and clarity**

- **One job per prompt.** If a single prompt asks the model to plan, execute, and summarise, those are three prompts. Flag prompts that mix orchestration with detail work.
- **Clear sectioning.** Role/context, task, constraints, output format, examples — these belong in distinct sections, not interleaved. Flag prompts where the reader cannot tell what is instruction vs context vs example.
- **Unambiguous output schema.** If the prompt expects JSON/markdown/a specific shape, the schema must be explicit (with field names, types, and an example). Flag prompts that say "return a JSON object" without naming the keys.
- **Stage budgets and caps.** When a prompt asks for multiple findings/items, an explicit cap ("up to 5", "no more than 3") prevents runaway output. Flag uncapped open-ended generation.

**Instruction quality**

- **Imperative, not aspirational.** "You MUST..." and "Do NOT..." beat "Try to..." and "You should consider...". Flag wishy-washy phrasing on critical instructions.
- **Examples that demonstrate the rule, not contradict it.** Flag examples that the surrounding rules would forbid, or examples that don't match the requested output format.
- **Negative examples ("do not do this") only with explanation.** A bad example without saying why-bad confuses the model.
- **No buried lede.** Critical constraints and output-format requirements should be at the top or in a clearly marked section, not in the middle of a wall of text.

**Robustness to model behaviour**

- **No injection paths.** User-controlled input must be clearly delimited (markdown fences, XML tags, role separation) — never concatenated into instructions. Flag prompts that splice raw user data into the instruction body.
- **Falsy-output handling.** A prompt that says "return findings" must also specify what to return when there are no findings (empty array, sentinel, etc.). Flag missing empty-case instructions.
- **"Do not invent" guardrails.** Prompts that ask the model to ground claims in evidence should explicitly say what to do when evidence is missing (skip, say "unknown", flag uncertainty).
- **Avoid speculative reasoning.** "Think step by step" / "Reason carefully" wording is fine; "Imagine you are a..." role-play framings that contradict the task are not.

**Cost and behaviour**

- **No unnecessary chain-of-thought when structured output is needed.** If the prompt expects strict JSON, asking for free-form reasoning before it usually breaks parsing.
- **Temperature and model assumptions documented.** If a prompt depends on `temperature=0` or a specific model, the file should say so (in a comment or frontmatter). Flag prompts whose behaviour will silently degrade on a different temperature/model.
- **Token budget awareness.** Flag prompts with large inlined boilerplate (200+ lines of repeated examples) where a shorter form would do — token cost recurs on every invocation.

**Maintenance**

- **Versioning visible.** Whether via filename suffix, frontmatter, or a comment, non-trivial prompts should carry a version. Flag a substantial rewrite that doesn't bump the version.
- **Diffability.** Wall-of-text prompts that change in many places at once are hard to review. Encourage one logical section per paragraph or per clearly-named heading.
- **No dead instructions.** Old rules that have been superseded but left in the file create contradictory guidance. Flag stale-feeling sections.

**Severity guidance for prompt comments**

- CRITICAL: prompt injection paths, broken output schema, contradictory instructions that will cause production failures.
- WARNING: unclear sections, missing edge-case handling, ambiguous schema.
- SUGGESTION: stylistic improvements, better example choice, version bump reminder.

### Output format unchanged

Continue to emit the same JSON-array output format as the rest of the review. Prompt-file findings go in the same array; anchor at the line of the prompt file (or the first changed line if the issue is structural).
