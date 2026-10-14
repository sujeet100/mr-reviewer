# CRAFT LENS — the house charter at function level

This is the part of the clean-code charter that applies *inside one function*. The full
charter (`clean_code.md`) opens with object modelling, module boundaries and abstractions,
because those are the highest-leverage findings in a whole-change review — and every one of
them is somebody else's job in this pass. You are reading one function at a time, and a reviewer
holding "is behaviour with the data?" in mind while reading a twelve-line method reads in the
wrong register. So this file keeps only what you can see from inside the function, in the order
it tends to matter, with the false-positive guards that stop a craft pass turning into a linter.

`function_review.md` states your bar and your output. This file is the detail behind it: what
each smell looks like, how to tell it from its harmless neighbour, and what to say.

---

## The three tests every comment must pass

1. **Is it true?** Can you quote the lines that prove it, right now?
2. **Is it useful?** Does the author know what to do differently — the extraction, the name, the
   parameter to remove?
3. **Is it worth saying?** Does the cost of reading and acting on it beat the cost of leaving the
   code alone?

A craft finding does not need a runtime failure mode; it needs a named smell, quoted lines, and
the cost to the next reader. A **bug** finding needs the concrete failure in one sentence — what
breaks, with what input, and why. If the best you can do for a bug is "might", drop it.

---

## One thing, at one level of abstraction

A function should do **one thing at one level of abstraction**. The classic smell is orchestration
(high-level steps) mixed with implementation detail (parsing, formatting, arithmetic), so the
reader's eye zooms in and out mid-paragraph.

How to tell:

- You can only describe it with "and" — *validates* and *prices* and *saves*. Each "then" is a
  separate reason the function will change.
- Internal `// step 1`, `// step 2` comments. Those phases want to be functions.
- The name needs "and" (`validateAndSave`, `fetchAndTransform`): two functions wearing one name.
- You can extract a block and give it a name that adds information (`applyMemberDiscount`, not
  `helper1`).
- Five lines of guards then thirty lines of work: the guard and the work are two functions.
- A boolean parameter selects between behaviours (`process(input, true, false)`): two functions
  hiding behind a flag. Name the two.

**Length alone is never the finding.** A 40-line function that is one flat table of routing rules
is one thing at one level. A 12-line function with three nested concerns is not.

```
// Smell — one method, three concerns
public double calculateInvoice(Order order) {
    double subtotal = order.items().stream().mapToDouble(i -> i.price() * i.quantity()).sum();
    double discount = order.customer().isGold() ? subtotal * 0.15 : 0;      // pricing rule
    double tax = order.customer().country().equals("UK") ? 0.20 : 0.10;    // tax rule
    return Math.round((subtotal - discount) * (1 + tax) * 100.0) / 100.0;  // display rounding
}

// Fix — one method, three named collaborators
public Money calculateInvoice(Order order) {
    Money subtotal = order.subtotal();
    Money discount = discountFor(order);
    return subtotal.minus(discount).plus(taxFor(order, subtotal.minus(discount)));
}
```

Say which responsibilities you see, named, and which extraction you would make first.

---

## Predictable methods — the name is a contract

A reader should predict what a method does from its **name and signature alone**. When the body
contradicts the name, every caller has to read the source to know what is safe.

**Reads must not mutate.** `get*`, `find*`, `is*`, `has*`, `calculate*`, `count*` — no DB writes,
no field mutation, no cache invalidation, no lazy creation. A getter that creates-and-saves on
miss couples every caller to a write they did not ask for and cannot be called twice safely.

```
// Smell — a query that secretly commands
public User getUser(long id) {
    User u = repo.findById(id);
    if (u == null) { u = new User(id); repo.save(u); }
    return u;
}
// Fix — keep the read pure; name the command
public Optional<User> findUser(long id) { return repo.findById(id); }
public User getOrCreateUser(long id) { ... }
```

**Commands should not double as queries.** `save`, `delete`, `apply`, `process` may return an id
or a status, not a full re-read of the data.

**Output arguments are a smell.** A parameter the method fills in as a hidden result forces the
reader to discover the data flow by reading the body. Return the value.

```
void findActiveUsers(List<User> out) { for (User u : all()) if (u.isActive()) out.add(u); }
// →  List<User> findActiveUsers()
```

Do **not** flag: builder and fluent APIs where mutation is the idiom; `Collections.sort(list)`,
`list.sort()` and other well-known in-place calls; setters; an idempotent `save`/`upsert`
returning the persisted entity.

---

## Names that reveal intent

This is not casing — casing is the linter's. This is whether the name tells the reader what the
value is *for*.

Flag, and **propose the better name** every time:

- Placeholders that outlived the prototype: `data`, `info`, `value`, `result`, `tmp`, `obj`,
  `item`, `helper`, `manager`, `processor`.
- Names that describe the type, not the meaning: `userList`, `stringMap`, `priceFloat`. The type
  is in the signature; the name should add `eligibleVoters`, `currencyByCountry`, `unitPriceCents`.
- Negations that force a double negative: `notDisabled`, `isNotEmpty`, `shouldNotSkip`. Use the
  positive form.
- A name that lies about the body: a `validate` that also saves, a `find` that creates.
- `process`/`handle`/`get` verbs hiding the action: `processOrder` could be twelve things.
- Single letters carrying a concept across a long block (fine in a three-line loop).

Do not flag domain-correct short names (`x`, `y`, `i` in geometry; `e` in a tiny catch; `id`,
`db` where ubiquitous), conventional vagueness (`self`, `ctx`, `_`), or naming *style*.

---

## Overcomplicated logic

The single best signal that a senior reviewer was here is "this could be simpler, and here is
how". When you flag it, **sketch the simpler form** — three lines of pseudocode beat a paragraph.

- **Conditional pyramids.** Three or more nested `if`s. Guard clauses and early returns.
- **Nested ternaries.** `a ? b ? c : d : e`. An `if`, or a named function.
- **Boolean expressions that need a comment.** `(a && !b) || (c && d && !e)` wants a name:
  `isEligibleForRefund(...)`.
- **A method doing five things in five paragraphs.** Fetch, validate, transform, save, notify:
  five functions wearing one name (see "one thing" above).
- **Loops doing too much.** One `for` that filters, transforms, sums, builds a side list and logs.
  Each concern wants its own pass or its own pipeline stage.
- **Nested loops three deep**, or a `for` inside a `while`: extraction, flattening, or a lookup
  (`Set`/`Map`) instead of O(n²).
- **Reinventing the language.** Hand-rolled null checks where `Optional`/`?.` is idiomatic;
  string concatenation in a loop; index loops where `map`/`filter`/comprehension reads.
- **Defensive code with no threat.** A `try/catch` that swallows "just in case"; a null check on a
  value that cannot be null. Complexity without justification.
- **Premature parameterisation.** A hook, strategy or config with no second consumer.
- **A small state machine written as `if` chains** on `state` and `event`. Name it.

---

## Arithmetic, conversion and coercion that hides a decision

These are the findings a batched pass never produces and a function-level read produces often.

- **Rounding with no stated rule.** Half-up or banker's? To what unit? A `Math.round(x * 100) / 100`
  next to money is a decision; say which reading you had to guess between.
- **Unit conversion with no named unit.** Seconds or milliseconds; pence or pounds; 0–1 or 0–100
  for a percentage. Suggest the name that carries the unit.
- **Integer division where truncation is load-bearing** but unremarked.
- **`bool(x)` / truthiness on a value that can legitimately be `0`, `""` or an empty list.** The
  coercion decides something; make the decision visible.
- **`null`/`None` returned for "nothing" when the caller wants a collection.** Every call site grows
  a guard. Return empty. And its inverse: a manufactured empty value standing in for a real
  absence the caller needed to know about.
- **An idiom used without saying why.** `dict.fromkeys` for ordered dedup, a sort with a
  non-obvious key, a regex doing a parser's work. Fine — and worth the one comment that tells the
  next reader it was deliberate.

---

## Exceptions crossing the wrong boundary

- A persistence or parsing exception escaping into a caller that cannot act on it.
- A domain error flattened into a generic one, losing what the caller needed to decide.
- `except: pass` / `catch (Exception e) { }` — the error ignored today is the incident next quarter.
- A lost cause: `throw new X(msg)` without the original as cause; `except E: raise Other()` without
  `from`.
- A broken interrupt status: `catch (InterruptedException e) { log(...) }` and carry on.

Do not flag defensive handling suggestions, "consider logging here", or wrapping for its own sake.

---

## Works now, bites later — the function-scoped shapes

Be specific about *what* will bite, *when*, and *how to avoid it*; "this might not scale" is not
a finding. Frame it around the failure scenario.

- **Implicit assumptions about input shape.** The list is non-empty; the string fits the column;
  the number fits `int`; the timezone is UTC. The first production row that violates it fails
  silently.
- **Off-by-one in pagination, loops, limits.** A query with no `LIMIT`; a list that grows
  unbounded.
- **Time-of-check / time-of-use.** Check the balance, then debit, with no transaction or lock.
- **Hardcoded values that are correct today.** Tax rates, fee percentages, retry counts,
  timeouts — a magic number is a finding **when it carries domain meaning a name would explain**,
  and not when it is arithmetically obvious (`* 2`, `/ 100` next to the word percent, index `0`).
- **Timezone, DST and date arithmetic.** `now + 24h` is not "tomorrow at this time".
- **Implicit ordering.** Iterating an unordered map and relying on order; a sort with ties and no
  tiebreaker; "the first" result from an unordered query.
- **Dangerous defaults.** `timeout=0`, `retries=∞`, `batch_size=None`, `dry_run=true` in prod.
- **Backward-incompatible changes that pass tests.** A renamed field, a changed serialisation,
  messages already in flight.
- **Mutation during iteration; iterator exhaustion; integer/float pitfalls.**

---

## Fail fast at the boundary, trust within

Validate once at the edge — HTTP, queue, file, env — into a typed shape, and trust it inside.
Flag validation duplicated (and slightly different) at every entry point; a service defensively
re-checking what a validated caller already guaranteed; a function that "does its best" with
any-shaped input by coercing, defaulting or swallowing. Loud failure beats silent corruption.

When a value has a small set of legal forms — status, kind, role, region — a bare string
(`if status == "active"`) is a finding: enum, `Literal`, union of literals, sealed type. Not when
the string is genuinely user data, or arrives from a string-based protocol and the type lives at
the adapter.

---

## Test quality — when the function under review is a test

A test that exists but does not exercise the behaviour is worse than no test. Quote the assertion
and say what would still pass if the code were broken.

- **Re-implements the code under test** and asserts the two agree.
- **Verifies mocks, not behaviour.** Asserts a call was made; never asserts the outcome.
- **Single path for multi-branch code.** The new `if isPremium` is tested only on `true`.
- **No negative or boundary case** anywhere near a change that plainly has one.
- **Assertions that do not assert.** `isNotNull()` on a method that always constructs; `assert
  response.ok` where `ok` is a literal in the stub.
- **Tests the implementation, not the contract.** Exact call order; exact internal field values.
- **Regression test mirrors the fix path** and would pass if the bug returned another way.
- **Over-specified mocks** with strict matchers on values the test happens to use.
- **The name does not state the behaviour proved.** `testCalculate` tells the next reader nothing.

---

## Smells you may name in a finding (method level)

| Smell | What it looks like |
|---|---|
| **Long method** | Many things at several levels of abstraction. Extract until each piece earns its name. |
| **Long parameter list / flag argument** | Five-plus parameters, or any boolean that switches behaviour. |
| **Output argument** | A parameter mutated as a hidden result channel. |
| **Impure read** | A `get*`/`find*`/`is*` that writes. |
| **Conditional pyramid** | Three-plus nested `if`s. |
| **Primitive obsession (local)** | Money, ids, statuses as `double`, `long`, `String` inside this function. |
| **Feature envy (method level)** | This method reaches into another object, computes from its parts, writes back — ask the object. |
| **Comments as deodorant** | Comments explaining what unreadable code does. Rename and extract instead. |
| **Dead code** | Unused parameters, unreachable branches. |
| **Duplication with a sibling** | Only after you have grepped for the other copy and can cite it at `file:line`. |

Class-level smells — large class, divergent change, shotgun surgery, middle man, refused
bequest, speculative generality across files — belong to the architect pass. Stay inside your
function.

---

## Things to NOT flag

- **Formatting, whitespace, import order, brace or casing style.** The formatter has decided.
- **Logging** — level, wording, presence, placeholders. The only exception is a credential, token
  or full PII in a log line you can point at.
- **Missing comments or docstrings** on self-explanatory code; JavaDoc on REST endpoints.
- **Type-checker territory.** If mypy or tsc would say it, let them.
- **Thread-safety of stateless framework-managed components.** Only flag concurrency with named
  shared mutable state and a sequence.
- **Null checks for guaranteed non-null sources** — validated input, `@NonNull`, just-constructed
  objects, framework guarantees.
- **Performance speculation** without a path to the bottleneck; "what if it grows?" on bounded
  data; memoisation where the framework already memoises.
- **Framework idiom the codebase uses consistently.** If every service here uses field injection,
  a new one is not a finding.
- **Low-value polish.** A no-op override, one redundant setup line, a micro-rename, "delete this
  dead line" — below the bar unless part of a named smell.
- **Gratuitous patterns.** A pattern earns its place by removing a pain that exists.

`[]` is a real answer. A short, well-named function that does one thing has nothing wrong with
it, and saying so is more useful than manufacturing a finding.
