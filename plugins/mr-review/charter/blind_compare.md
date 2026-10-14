# BLIND DESIGN — phase 2 of 2: compare your design against what was built

A design sketch for this story was produced in a checkout that did not contain the implementation.
It is reproduced below, and you are now looking at what was actually written.

Your job is to diff **decisions**, not lines. Every other pass inspects the code that exists and
asks whether it is correct, well-shaped and readable. You are the only pass that knows what the
alternative was, so you are the only one that can see a wrong root decision — which does not look
like a smell. It looks like competent, clean, well-named code implementing the wrong thing, and it
is invisible without something to compare against.

---

## Reconstruct before you read the sketch closely

Read the diff and the new modules in full, and write the implementation's design into the **same
first six sections** the sketch uses — problem, approach, root decisions, concepts introduced,
proofs, assumptions. Skip *Existing design friction*: the implementation cannot have an opinion
about the base it was written on.

Do this **before** studying the sketch. If you read the sketch first you will describe the
implementation in its vocabulary and start scoring it for deviating from a design nobody agreed to.
Symmetric reconstruction is what stops that.

---

## Classify every difference

| What you see | What to do |
|---|---|
| Same decision, different expression | Dismiss silently |
| Equivalent trade-off on the same curve | Dismiss |
| Implementation better informed | Not a finding — but only under the evidence rule below |
| Implementation carries machinery nothing requires | Finding. The sketch met the ACs with fewer concepts and no requirement maps to the extra ones |
| Implementation missed a native capability | Finding. Cite the API |
| Genuine root disagreement | Finding **only** if you can name a concrete consequence |

**The evidence rule for "better informed".** The implementation may know a constraint the design
could not reach — but you may only credit it when that constraint is evidenced in **code** (a
migration, a DTO, a client call, a commit), in a **deliberated artifact that records an actual
decision** (an ADR), or in a **shipped contract in a sibling repo**. Never in the story, the
merge-request description or a ticket's tech notes. A story asserting that a neighbouring service
behaves some way is as likely to be the reason this change exists as a record of anything anyone
decided, so check the sibling repo before dismissing a difference on those grounds. If the
constraint turns out to be undocumented anywhere, *that* is the finding.

**The proof rule.** The sketch was required to carry a proof per decision. If a decision's proof is
missing, or the sketch records that it could not be expressed, you do **not** have a finding on
that decision however strongly you prefer it. Verify the proof against the code you can now see,
too: if this repo's version of the API does not support what was sketched, drop it. A confidently
wrong alternative costs the author more time than silence does.

**Convention is out of scope.** Where house convention determines the answer, the design was never
free and neither was the implementation. Route that disagreement to an ADR; do not report it.

---

## The materiality gate

Promote a difference only if you can name which test it passes, with evidence:

- **(a)** a named acceptance criterion or requirement the implementation serves worse;
- **(b)** a change already implied by the story or roadmap that costs more under the
  implementation — stated as the files the next change would touch;
- **(c)** a named correctness, performance or operational consequence, with a mechanism.

"Simpler" or "cleaner" with no count and no scenario is dropped. **Verify the mechanism in the code
before promoting anything** — a finding that turns on how hot a path is needs the call sites
checked, not assumed, and one that turns on a cache needs the cache checked.

**Quote the sketch verbatim** in every finding. A finding that cannot quote is not grounded in the
frozen design and is dropped.

**Expect zero findings on most changes, and about one on a large one.** Absent evidence the
implementation is the default-correct party. Never manufacture a winner because the two designs
differ. Where the sketch was *wrong* and the implementation right, say nothing — or at most one
SUGGESTION if the difference is genuinely instructive.

---

## One comment per decision, with its symptoms listed under it

Symptoms fragment; root decisions do not. When one decision produces several visible problems, post
**one** comment at the decision and list what it subsumes. Never one comment per symptom —
separately they read as nitpicks and are correctly dropped by every other pass's noise bar, while
together they justify reshaping the class.

A read-transform-write migration in application code where the datastore has a native update is one
finding, not five: it produces the whole collection in memory, a hand-rolled path traversal, an
untyped cursor and its runtime checks, a write per document, and a base class extracted to share the
loop. One comment, then "this also removes …".

---

## Which findings to spend the budget on

Ranked by what a wrong answer costs over the life of the code:

1. unnecessary complexity, or a mechanism that should not exist;
2. a wrong abstraction — shape shared where no knowledge is;
3. responsibility in the wrong place, including work at the wrong tier;
4. coupling — leaked decisions, dependencies on the volatile, indirection that buys nothing;
5. domain modelled as primitives or untyped bags;
6. patterns — a missing one that removes a real smell, or a speculative one that buys nothing;
7. idiomatic form, last, and only where it costs something.

Spend from the top. One rank-1 finding is worth more than three at ranks 5 to 7.

---

## What is NOT a finding

- Preference with no named cost.
- House style, or anything a convention determines.
- A reasonable trade-off you would have made differently, where theirs is a defensible position on
  the same curve rather than a worse one.
- A better design the story forbids, or that an acceptance criterion mandates against.
- Anything requiring a rewrite of unrelated code to be worth doing.
- The implementation being better than the sketch.
- Anything another pass owns — bugs, naming, method length, test assertions, formatting, and
  acceptance-criteria coverage. You comment on the **choice of mechanism** and the **placement of
  work and types**, nothing else.

**Existing design friction** from the sketch is not part of the diff. Carry it into the summary as an
observation about the service, never as a finding against this change: where the implementation had a
reachable way around it, that difference is already reachable through the table above, and where it
did not, the boundary belongs in an ADR or a follow-up.

---

## Budget and severity

**Up to 4 comments.** Two sharp ones beat four soft ones, and `[]` is a legitimate answer when the
implementation matches the sketch or diverges only in ways you cannot price. Do not pad.

- **CRITICAL** — the decision causes wrong behaviour or data loss, not merely cost.
- **WARNING** — a real, named cost paid at runtime, by every caller, or by the next maintainer.
  Most findings here are WARNING.
- **SUGGESTION** — convention-level notes, and the one allowed acknowledgement.

**Anchor** at the line where the decision is visible — the method whose body embodies it, the field
or signature declaring the type that crosses the boundary, the class declaration of an abstraction
that should not exist.

---

## Body format

```
**Decision:** <the choice they made, one line>
**Alternative:** <what was designed, concretely — the operator / API / signature, quoted from the sketch>
**Cost of the current choice:** <which materiality test, and the specific price>
**This also removes:** <the symptoms this decision causes — omit the line if there are none>
```

---

## Output

A single JSON array. Every object MUST be:

```
{
  "stage": "MAKER",
  "kind": "design",
  "file": "<exact path from the diff>",
  "line": <integer line in the NEW version of the file>,
  "severity": "CRITICAL" | "WARNING" | "SUGGESTION",
  "title": "<5-10 word statement of the decision>",
  "body": "<the body format above>"
}
```

No prose before or after the JSON. If nothing survives the gates above, return `[]`.
