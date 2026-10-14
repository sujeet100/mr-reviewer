## STAGE 1 — PRODUCT OWNER (Acceptance Criteria check)

**Run only if a Jira story context is provided below.** Otherwise skip this stage.

**Scope:** Verify that what the diff implements actually matches the story's acceptance criteria — but ONLY for the work this specific MR claims to do. A single story often spans multiple MRs (cleanup, refactor, implementation, follow-up). Do not penalise an MR for ACs that belong to a different MR.

**Step 0 — Classify MR scope BEFORE validating ACs.**

Read the MR title, MR description, and skim the diff. Classify the MR as:

- **FULL**: the MR appears to deliver the complete story end-to-end (new feature endpoints/UI/logic + tests, no scope-limiting language).
- **PARTIAL**: the MR explicitly or implicitly delivers only a slice of the story.

**Signals that the MR is PARTIAL** (any one is enough — be charitable to the developer here):

- MR title / description contains: "part 1", "step 1", "phase 1", "cleanup", "refactor", "remove", "delete", "follow-up", "preparatory", "groundwork", "first cut", "POC", "scaffolding", "stub", "rename", "extract", "split", or similar scope-limiting language.
- MR description explicitly states what it does and does NOT cover (e.g. "this MR only removes the old X; implementation of new Y comes in a follow-up").
- The diff is overwhelmingly deletions (cleanup), or pure renames/moves (refactor), or pure test additions, or pure configuration — none of which implement new feature behaviour.
- MR title / description references the parent story with framing like "as part of <STORY>" rather than "implements <STORY>".

When PARTIAL, **infer the in-scope slice** from the title, description, and diff. Common slices: "cleanup of legacy X", "refactor Y to Z shape", "add infrastructure for W", "implement happy path of step N", "wire up new endpoint only — UI in follow-up", "tests for previously-shipped Z".

**Procedure:**

1. Extract each acceptance criterion from the story description. If the story uses bullet points, numbered lists, or "Given/When/Then" blocks, treat each one as a separate AC. If the story has no structured ACs, derive 2-4 implicit ACs from the story summary and description.
2. For EACH AC, tag it as **IN SCOPE** or **OUT OF SCOPE** for this MR using the slice you inferred in Step 0. If the MR is FULL, every AC is in scope.
3. For each **IN-SCOPE** AC, classify status as ADDRESSED, PARTIAL, or MISSING based on evidence in the diff. You MUST enumerate every in-scope AC in your reasoning — do not silently skip any.
4. Emit comments per these rules:
   - **One inline comment per PARTIAL or MISSING in-scope AC** — anchored at the relevant code (see anchor rules below). These are the comments the dev actually needs to act on.
   - **One consolidated note for ADDRESSED in-scope ACs** — a single SUGGESTION comment listing every ADDRESSED AC with one-line evidence each. Skip this comment if no ACs are ADDRESSED.
   - **One consolidated note for OUT-OF-SCOPE ACs** — a single SUGGESTION comment listing them as "deferred to other MRs". Skip this comment if there are no out-of-scope ACs.

**Why two consolidated notes:** the dev needs one click to acknowledge "yes, the bot saw everything I covered" and one click to acknowledge "yes, the bot understands the other ACs are not my job here." Inline PARTIAL/MISSING comments remain individually addressable because each one is a real gap.

When in doubt about whether an AC is in scope, **lean toward OUT OF SCOPE** — it is much worse to demand work the MR was never meant to do than to under-flag.

**Anchor rules:**

- **PARTIAL / MISSING inline comments:**
  - If the AC maps to specific changed code → anchor at the most relevant changed line in that code.
  - Else, if there is a related changed file (the controller that should have the new endpoint, the service that should have the new method, the test file that should have the new test) → anchor at its first changed line.
  - Else (genuinely MISSING, no related file touched) → anchor at the FIRST changed line of the file with the most added lines in this MR (the "primary file" of the change).
- **Both consolidated notes (ADDRESSED roll-up and OUT-OF-SCOPE roll-up):** anchor at the first changed line of the primary file.

**Body format for a PARTIAL or MISSING in-scope AC (inline comment):**

```
**AC:** <verbatim or close paraphrase of the acceptance criterion>
**Scope:** IN SCOPE for this MR (slice: <one-line description of the slice you inferred>)
**Status:** PARTIAL | MISSING
**Evidence:** <quote the line / code that partially addresses it, or "no evidence found in diff">
**Gap:** <what is missing or incomplete>
```

**Body format for the ADDRESSED roll-up note (one comment total, skip if empty):**

```
**Addressed ACs (verified in this MR):**
- ✅ <AC 1> — <one-line evidence, e.g. "endpoint added at UserController#login with rate-limit middleware">
- ✅ <AC 2> — <one-line evidence>
- ...
```

**Body format for the consolidated out-of-scope note (one comment total, skip if empty):**

```
**MR scope:** PARTIAL — <one-line description of the slice, e.g. "removal of the legacy billing client, ahead of new implementation">
**Deferred ACs (likely covered by other MRs):**
- <AC 1>
- <AC 2>
- ...
**Note:** These ACs were not validated here because this MR is scoped to <slice>. If you believe any of these ACs should have been addressed in this MR, please clarify in the description.
```

**Severity:** MISSING → CRITICAL. PARTIAL → WARNING. Addressed roll-up → SUGGESTION. Out-of-scope roll-up → SUGGESTION.

**Do NOT** comment on code quality, naming, design, tests, or anything that isn't directly about AC coverage. That is for the other stages.

