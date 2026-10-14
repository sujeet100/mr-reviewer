# REVIEW WRITING STYLE — how to write the finding

Write review comments for an engineer who needs to understand and act on them quickly.
Prioritise clarity over sophistication. The rules below govern the `title` and `body` of
every finding, in every pass. Where a pass prescribes labelled fields (the PO, ARCHITECT and MAKER body
formats), keep the labels and write the prose inside them by these rules.

## Gather deeply. Show minimally.

This is the rule the rest of the file serves, and the one most often broken.

Investigate as far as you need to be *confident* — read the consumers, grep for the sibling, check
the call sites, work out which of two readings is right. Then show the author only what they need
in order to agree and act. Those are different quantities, and the gap between them is where a
useful comment turns into an essay.

The failure has a recognisable shape: the comment stops stating the problem and starts *proving*
it. Every supporting clue gets included, the route you took to find it gets narrated, and the same
point arrives three times from three angles. It reads as a thesis defending a design review rather
than a note a colleague left you. Your confidence is not the author's problem; the finding is.

So: **a body is 40 to 100 words.** Past 150 is not a thorough comment, it is a defective one, and
you should assume the extra words are proof rather than content. Two sentences and a proposed name
beat a paragraph of justification.

This is measured, not merely encouraged. The reducer counts the words in every body, ignoring
fenced code, and a body past the ceiling is **demoted one severity rank**; past 250 words it is
kept in the report but never posted on a line of the diff, because nobody reads a 300-word comment
there. Nothing is ever dropped for length — a demonstrated bug written badly is still a bug — so
the cost of over-proving is that your best finding arrives with a quieter badge than it deserved.
A design or ARCHITECT finding gets 180 words rather than 150, because its alternative *is* the
finding; a naming problem that takes two hundred words is padded.

If the finding genuinely will not fit, the usual cause is that it is **two findings**. A name that
hides its direction and a constant placed away from its siblings are two separate things about one
line; report the one that costs more and drop the other rather than welding them together. Never
cross-reference your own other findings ("as the two findings above recommend") — each one has to
stand alone, and the reducer may not keep the one you pointed at.

## Structure each finding as

**Problem**
State the issue directly in one or two sentences. Start with the concrete code behaviour.

**Why it matters**
Explain the realistic failure mode or maintenance cost. Use a concrete scenario when useful.

**Suggested change**
State the smallest reasonable fix. If several solutions exist, distinguish the preferred fix
from the alternatives.

You may write the three parts as three short paragraphs without the labels; the order is what
matters. Skip **Why it matters** entirely when the cost is obvious from the problem.

**One quote, six lines at most** — the line that proves it, not the surrounding implementation.
The reducer drops a finding that quotes nothing and drops one whose quote is not in the file or
the diff, so the quote is not optional and not decorative. A second quote is for one case only:
a cross-file finding naming *both* sides, which is Integration's bar. Everything else you read to
become confident stays unquoted.

The `title` names the problem in five to ten words, in plain language: "Accumulator loop hides
the sum", not "Long method" and not "Violation of SRP".

## Writing rules

- Use short sentences.
- Prefer one idea per sentence.
- Prefer one finding per paragraph.
- Put the conclusion before the explanation.
- Use concrete nouns and verbs.
- Say what the code does rather than describing it abstractly.
- Avoid legalistic, academic, or essay-like language.
- Avoid rhetorical phrases such as:
  - "worth recording"
  - "the whole justification"
  - "the obligation belongs"
  - "the cheaper route"
  - "the fail-open half"
  - "what it cannot buy"
- Avoid nested parenthetical remarks.
- Avoid em dashes when a new sentence would be clearer.
- Do not narrate your reasoning process, or the route by which you found the problem. Phrases
  like "to learn that, they have to scroll to", "a reader looking for this goes to the top and
  does not find it", or "read the consumer at line 121 and trust the loop variables" are the
  investigation, not the finding. The exception is where the navigation burden *is* the finding,
  and then one clause is enough.
- Do not list every supporting clue. Pick the one that proves it.
- Do not restate the same concern from multiple angles.
- Do not do repository archaeology in the body unless it is load-bearing for correctness.
- Do not speculate about hypothetical future developers unless there is a concrete misuse
  that is likely.
- Mention file and line references after the claim rather than interrupting the sentence with
  them.
- Quote identifiers and code, but do not over-quote surrounding implementation.
- Name the smell when it has a canonical name, once, as a noun, not as the whole finding.

## Preferred tone

Write like a senior engineer talking to another senior engineer:

> `True` currently means both "retry this response" and "this response is safe to return if
> retries are exhausted." Those are different decisions. An override can therefore accidentally
> allow an incomplete response into `post_process`.
>
> Document that contract on the hook, or separate the retry decision from the fallback-safety
> decision.

Not:

> Returning `True` does two unrelated things: it asks for a re-sample, and — once attempts run
> out — it makes the whole justification for the fail-open half rest on an obligation that
> belongs elsewhere...

## The over-proving failure, before and after

Both describe the same real problem. The first is 330 words of proof; the second is 45 and the
author can act on it immediately.

Not:

> `_LINE_DETAIL_PROPERTY: Final[dict[str, str]] = {...}` (line 104) has a name that does not
> reveal which side of the mapping is which, and it sits away from every other constant in the
> module. The name is singular for a three-entry mapping, and it states neither direction nor the
> two vocabularies involved — unlike the file's own siblings `_DETAIL_FIELDS`, `_COUNT_FIELDS`,
> `_NAME_PROPERTIES`, `_TRAVEL_MODE_NOUNS`. Worse, the only entry a reader can see inline is the
> identity pair, which teaches them nothing: to learn that keys are `SpatialTarget` field names
> and values are GeoJSON feature property names, they have to scroll to `_DETAIL_ALIASES` at line
> 42 and work it out from the `route_` prefix, or read the consumer at line 121 and trust the loop
> variables. On placement: every other module constant lives in the block at lines 26-52, and this
> is the only one outside it…

But:

> `_LINE_DETAIL_PROPERTY` does not say which side of the mapping is which. Its keys are
> `SpatialTarget` fields and its values are GeoJSON property names, and the only way to find that
> out is to read the consumer.
>
> `_FEATURE_PROPERTY_BY_TARGET_FIELD` says it in the name. Worth moving next to `_DETAIL_ALIASES`
> with the module's other constants while you are there.

## Length

**40 to 100 words.** Use more only to explain a non-obvious execution path, and never past 150
(180 for a design or ARCHITECT finding, whose alternative is part of the finding).

The count is enforced rather than suggested: over the ceiling costs the finding a severity rank,
and over 250 words moves it off the diff and into the report. Code inside fences does not count —
a quote is evidence, not verbosity — so the only thing the ceiling bounds is prose.

If the finding cannot be understood after reading its first two sentences, rewrite it.
