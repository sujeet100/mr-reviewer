You proposed the following review comments for this MR. Re-evaluate each one **strictly** against the diff. Your job is to drop weak findings before they reach the developer.

A senior reviewer's review is judged by what they chose NOT to post, more than by what they did post. Be ruthless.

---

## DROP a comment if ANY of these are true

1. **Ungrounded** — the claim is not demonstrable from the actual code in the diff. You cannot quote the exact line that proves the issue.
2. **Speculative** — the body uses hedges like "might be", "could be", "what if" for a *bug/security/concurrency* claim. Senior reviewers post concrete failure modes, not worries. (This does NOT apply to craft findings — see the craft carve-out below.)
3. **Wrong target** — the comment is about unchanged code, framework boilerplate, or code that isn't actually in this MR.
4. **Lint-tier** — naming style, log level, formatting, magic numbers, `print`/`console.log` left in code, parameterised vs concat logging, AssertJ-vs-JUnit, generic variable names. CI/linters handle these.
5. **Logging noise** — any comment about log levels, log wording, missing/extra log statements. (The ONLY logging issue worth posting is sensitive data in logs.)
6. **Theoretical security** — "could be exploited if the input were attacker-controlled" when it clearly isn't. "Should use `SecureRandom`" for non-security IDs. "Could be a path traversal" without evidence the input is user-supplied. Drop unless you can name the attacker action.
7. **Theoretical concurrency** — thread-safety concerns on stateless framework-managed components (Spring `@Service`, FastAPI views, Angular services). "Might race" on method-local variables. Drop unless you can name the two threads and the shared mutable state.
8. **Theoretical performance** — Big-O concerns on bounded data, "what if it grows?", premature-optimisation suggestions. Drop unless you can demonstrate the bottleneck is in this code's hot path today.
9. **Defensive null/validation suggestions** where the source is guaranteed non-null (validated input, `@NonNull`, framework guarantee, just-constructed object).
10. **Style/preference dressed as a finding** — "could be more readable as...", "prefer X over Y" without a named defect. (NOTE: this does NOT mean drop all craft. A craft finding that *names a canonical smell* — long method, output argument, impure read, conditional pyramid, feature envy, misleading/negated name — and *quotes the lines that prove it* is a real finding; KEEP it. Only drop vague "could be cleaner" comments with no named smell and no quoted evidence.)
11. **Duplicate** — overlaps with another comment that says the same thing. Keep the sharpest one; drop the rest.
12. **Restates the obvious** — "this method is doing a lot" without naming the responsibilities. "This name could be clearer" without proposing the better name.

---

## KEEP a comment if ALL of these are true

1. You can **quote the exact line** in the diff or referenced code that demonstrates the issue.
2. Either: you can describe the **concrete failure mode** in one sentence (for a bug/security/concurrency finding) — OR it is a **craft finding** that names a canonical smell and quotes the lines that prove it (see carve-out below).
3. The fix is **actionable** — the developer knows what to change.
4. A respected senior engineer would post this comment on this MR.

---

## CRAFT CARVE-OUT (do not over-prune the function-level lens)

Findings tagged `"kind": "craft"` (or clearly about readability/maintainability — long method, mixed levels of abstraction, output argument, impure read, conditional pyramid, feature envy, misleading/negated name, method-level Tell-Don't-Ask) **do NOT need a runtime failure mode.** Their cost is on the next reader six months from now, not on production. KEEP a craft finding when it names the smell and quotes the lines; only drop it if it is vague ("could be cleaner" with no named smell), unquotable, or a pure casing/style nit. Do not apply the bug-class "concrete failure mode or drop it" bar to craft.

---

## OUTPUT

Return a JSON array of the KEPT comments. Each kept comment preserves its original fields (`stage`, `kind`, `file`, `line`, `severity`, `title`, `body`).

You may **tighten** a kept comment's `body` — sharpen the failure mode, trim hedges, propose the concrete fix — but do not invent new findings.

If every proposed comment fails the bar, return `[]`. Silence on a clean MR is a valid outcome.

Output ONLY the JSON array. No prose.

---

## DIFF (for reference)

```diff
{diff_content}
```

---

## PROPOSED COMMENTS

```json
{comments_json}
```
