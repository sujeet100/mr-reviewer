# DESIGN PRINCIPLES

The bar a design is judged against. Language- and framework-agnostic on purpose: these are the
principles you design *toward*, not a style guide.

Two sources feed this file. The canonical literature — Parnas, Martin, Fowler, Evans, Beck, Metz,
Ousterhout, Hunt & Thomas, Meyer — supplies the vocabulary and the operational tests, and each item
below names its origin so a review comment can point at something older than an opinion. The house
bar from `coding-principles.md` (the Vicaris plugin's understand / implement / review charter) supplies
the local emphases, folded in where they apply. Kept in this repo so the reviewer never depends on a
path outside it.

`clean_code.md` is the same ideas written as things to *look for* while reading code. This file is
written as things to *decide* while designing it.

**Match the response to the request.** A throwaway or exploratory change gets the change and a
one-line note of what it breaks. A change that ships gets the whole bar. Do not design a
shipping-grade mechanism for a scratch requirement.

---

## 0. The two questions that come first

Before any principle applies, two questions decide whether there is anything to design.

**Does this need to exist at all?** (YAGNI — Fowler; "Over-engineering and speculative abstraction",
house bar.) The cheapest mechanism is the one you do not build. Can the data be deleted rather than
migrated? Is the old shape still readable, making the backfill optional? Does the framework,
database, or language already do this? Does one line of configuration replace the class? Ask "do we
actually need this?" and default to no.

**Is the complexity essential or accidental?** (Brooks, *No Silver Bullet*.) Essential complexity is
the problem's own difficulty and cannot be designed away. Accidental complexity is what the solution
adds. Every finding worth making is about accidental complexity — so before proposing an
alternative, be sure you are removing accident and not just relocating essence.

---

## 1. Complexity — what actually makes code hard

Ousterhout (*A Philosophy of Software Design*) gives the most usable definition: complexity is
**dependencies** (you cannot understand this piece in isolation) plus **obscurity** (important
information is not apparent). Both are the reader's problem, which is why "it works" is never the
standard.

- **Deep modules over shallow ones.** A module's value is the ratio of the functionality it provides
  to the size of the interface you must learn. A deep module has a small interface hiding a powerful
  implementation. A shallow one has an interface almost as complicated as doing the work yourself,
  so it hides nothing and only adds a hop.
- **Pull complexity downward.** When complexity is unavoidable, it belongs inside the module, not in
  every caller. The implementer suffers so the users do not. A configuration parameter that every
  caller must reason about is usually complexity pushed upward — compute a sensible value instead.
- **Beware classitis.** "Classes are good, so more classes are better" is false. Many tiny shallow
  classes are individually simple and collectively worse: each adds an interface, and the interfaces
  accumulate into system-level complexity. This is the direct counterweight to "extract everything".
- **Define errors out of existence.** Prefer a design where the error case cannot arise over one that
  reports it up the stack. But do not overreach: silently absorbing a real failure is the opposite
  principle, and a default that substitutes a plausible value for a missing one is a lie.
- **Simple is not the same as easy** (Hickey, *Simple Made Easy*). Simple means one concern, not
  braided together with others. Easy means familiar or near at hand. Code that is easy to write is
  frequently complex — it *complects* concerns that then cannot be reasoned about separately.
- **Volume is complexity too** (Moseley & Marks, *Out of the Tar Pit*): the three big sources are
  mutable state, control flow, and sheer code volume. Fewer moving parts beats better-organised ones.

Two tests turn the definition into something you can actually apply:

- **Locality of reasoning.** Can this module be understood on its own, or must you mentally execute
  three others to know what it does? How much context has to be loaded before the code makes sense?
  Behaviour that is scattered fails this test even when every individual piece is tidy, and it is one
  of the strongest predictors of how expensive the next change will be.
- **The cost of indirection.** Every layer, interface, wrapper, adapter, factory, and abstraction is
  a hop the reader must follow, so each one has to buy something. Ask what this level of indirection
  buys *today*. If the answer is nothing, it is cost with no return — remove it. This is the
  even-handed version of "extract until you drop": extraction is not free, and the fee is paid by
  every future reader.

---

## 2. Where responsibility lives — responsibility, cohesion, boundaries, dependencies

SRP is not "a class should be small" and not "a function should do one thing" — both are widely
misquoted. Martin's own refinement (*Clean Architecture*) is: **a module should have one reason to
change, which means it should be answerable to one actor.** Gather what changes for the same reason;
separate what changes for different reasons.

The value of this framing is that it is observable in the codebase, through Fowler's two smells:

- **Divergent Change** — one module changes for several unrelated reasons. Too many
  responsibilities; split along the reasons.
- **Shotgun Surgery** — one conceptual change requires edits in many modules. Responsibility is
  scattered; gather it. The house corollary: *if extending something requires touching three files in
  coordinated ways, the abstraction is wrong, not the developer.*

Related smells at this altitude: Large Class, Long Method, Long Parameter List, Data Clumps
(a group of values that always travel together is an unnamed type).

**Cohesion is the easier question, and often the more useful one.** A module can be answerable to one
actor and still feel wrong. Ask instead: do these things naturally belong together, and would you
expect to find this responsibility here if you went looking for it? Incohesion is usually recognisable
on sight where an SRP violation takes an argument, so lead with cohesion and reach for SRP when you
need to explain *why*.

**Design for the change that is actually likely** (Parnas). This is not the same as avoiding
speculative generality, and the two are often confused. Speculative generality is building for a
change nobody has asked for. Designing for change is asking which changes this area *does* keep
receiving — a new region, a new data source, a new channel — and making sure the design isolates
those rather than scattering them. The operational test: name the next expected change, then count the
files it touches. If a routine, expected change is shotgun surgery, the boundaries are wrong.

**Architectural boundaries — is responsibility crossing a seam it shouldn't?** Most systems have a
handful of real seams, and this catches design problems SRP does not:

- transport ↔ domain (a controller deciding business rules; a DTO shape reaching into the domain)
- domain ↔ persistence (the domain knowing storage field names, query shapes, or migration concerns)
- client / integration ↔ caller (a client returning the wire format so every caller decodes it)
- orchestration ↔ computation (a service doing the arithmetic instead of coordinating the things that do)

**Dependency direction — do dependencies point toward the stable?** (Martin's stable-dependencies and
stable-abstractions principles, minus the architecture-style dogma.) High-level policy should not
depend on low-level detail; the volatile should depend on the stable, not the reverse. Ask whether
business logic is coupled to infrastructure, whether the domain knows about persistence or transport,
and whether anything central and widely-depended-upon is also the thing most likely to change. A
dependency cycle is always a design finding.

**Separate policy from mechanism.** A business rule should not know how it is stored, transported, or
scheduled. `calculateDiscount()` and `saveDiscount()` are two decisions; a `calculateDiscount()` that
branches on the storage backend has braided them, and neither can now be changed or tested alone. This
is the general form of the wrong-tier finding: policy leaking into mechanism, or mechanism leaking
upward into policy.

---

## 3. Information hiding, and abstractions that earn their place

**Information hiding is the foundation of modular design** (Parnas), and it is stronger than
encapsulation — encapsulation is a language mechanism, information hiding is a design decision about
*which choices are allowed to change without affecting anything else*. Every module should own some
decisions privately: a storage layout, a wire format, an algorithm, a caching policy.

The three questions to ask of any module:

- What implementation decisions does it currently expose?
- What should it own privately, and does its interface let it?
- **If its implementation changed tomorrow, how many callers would change?** If the answer is "all of
  them", it is hiding nothing, whatever its access modifiers say.

**An abstraction hides a secret, not a step** (Parnas, *On the Criteria To Be Used in Decomposing
Systems into Modules*). Decompose by what each module keeps private — a format, a storage layout, an
algorithm choice — not by the sequence of operations in a flowchart. A module named after a step in a
process is usually a shallow module.

- **Rule of Three** (Don Roberts, via Fowler's *Refactoring*). One occurrence: write it. Two: note
  the duplication and wait. Three: now you have evidence of the shape and can abstract from it.
  Abstracting at one or two is guessing.
- **DRY is about knowledge, not text** (Hunt & Thomas, *The Pragmatic Programmer*; clarified
  repeatedly by Thomas since). Two fragments that look alike but encode different decisions are not
  duplication, and merging them couples things that will diverge. Ask whether the two sites would
  *have to* change together. If not, the resemblance is coincidence.
- **General-purpose beats special-purpose** (Ousterhout). A somewhat general interface is usually
  both simpler and deeper than a narrowly-tailored one, because the special case leaks the caller's
  situation into the module.
- **Leaky abstraction.** If callers must know what the abstraction was meant to hide, it has failed —
  it now costs a hop and buys nothing.
- **Speculative Generality** (Fowler). Provider classes, config hooks, parameter threading, and
  interfaces with one implementation, all for a second caller that does not exist. The house bar is
  blunt here: default to no.

---

## 4. Wrong abstractions — the expensive failure

**"Duplication is far cheaper than the wrong abstraction"** (Sandi Metz, *The Wrong Abstraction*).
This is the most under-applied principle in the list, because a wrong abstraction does not look
wrong — it looks like DRY was correctly applied.

Metz's account of how one forms: someone spots duplication and extracts it; time passes; a new
requirement is *almost* the same, so a parameter and a conditional are added to make it fit; repeat;
and the shared thing is now incomprehensible while nobody feels able to unwind it because of the
effort already invested.

**The tells, in order of reliability:**

- Parameters or flags exist only so one caller can opt out of part of the shared behaviour.
- Conditionals inside the shared code branch on *which caller* is calling.
- Callers pass `null`, empty, or a placeholder for arguments that do not apply to them.
- The abstraction was extracted from two sites in the same change that created the second site — the
  second copy was evidence to re-examine the shape, not evidence of a shape.
- The thing shared is a *procedure skeleton* (find, loop, transform, write) rather than a decision.
  Sharing a shape couples code that has no knowledge in common.

**The remedy** (Metz): inline the abstraction back into each caller, delete the parts irrelevant to
that caller, and let the duplication show what the real abstraction is. "The fastest way forward is
back." Do not preserve a wrong abstraction out of sunk cost.

**Connascence** (Page-Jones) is the precise tool when you need to argue about coupling. Two elements
are connascent if changing one requires changing the other. Static forms, weakest to strongest: name,
type, meaning, position, algorithm. Dynamic forms: execution order, timing, value, identity. Judge
any coupling on three axes — **strength** (how hard to discover and refactor), **degree** (how many
elements share it), and **locality** (how far apart they sit). Strong connascence at long distance is
the thing to fix; weak connascence between adjacent lines is fine.

---

## 5. Domain modelling

- **Rich models, not anemic ones** (Fowler, *AnemicDomainModel*; Evans, *DDD*). Behaviour belongs on
  the object that owns the data. A service that reads an object's fields, decides, and writes them
  back has taken the object's job. The service orchestrates; the domain decides.
- **Tell, don't ask.** Do not query state, branch on it, and call back with the conclusion. Push the
  decision to the object holding the state.
- **Invariants live with the data.** Aggregates enforce their own rules (Evans). If every caller must
  remember to validate, the rule is in the wrong place.
- **Primitive Obsession** (Fowler) and **domain modelling over primitives** (house bar). A closed set
  is an enum or a literal union, never a bare string. An identifier is a type, not a `String`. A
  measurement is a type carrying its unit — a pixel count is not a `Double`, a money amount is not a
  `float`. Value objects are cheap and remove whole classes of error.
- **Type every boundary, trust types within** (house bar). The layer that knows the wire format owns
  the decoding, so its callers get a type. Handing back `Object`, `Map<String, Object>`, `Document`,
  `any`, or a bare dict from a layer that knows the shape pushes decoding onto every caller — which
  then interrogates types at runtime and invents a default for the missing key, and that default lies.
- **Ubiquitous language** (Evans). Names in code match the names the domain experts use. A concept
  the business has a word for and the code does not is a missing type.

---

## 6. OOP and FP — choosing per problem, not per allegiance

Feathers puts the trade cleanly: **OO makes code understandable by encapsulating moving parts; FP
makes code understandable by minimising moving parts.** Both are legitimate; the question is which
the problem wants.

- **Pure functions for business logic** (house bar). Validation, transforms, and calculation take
  arguments and return values, with no hidden I/O and no clock or environment read inside. Purity is a
  design property, not a testing one — it is what makes the logic mock-free to test.
- **Command-Query Separation** (Meyer). A function returns a value *or* changes state, never both. A
  `get`/`find`/`is`/`calculate` that writes is the canonical violation, and so is an output argument —
  a parameter mutated as a hidden second return.
- **Favour immutability.** Build a new value and return it rather than mutating a passed-in
  collection. Rebinding reads better than in-place mutation almost everywhere.
- **Encapsulate what varies; isolate what has state.** Objects earn their place where identity,
  invariants, and polymorphic behaviour matter. Functions earn theirs where a transform is the whole
  job. A class with no state and one method is a function wearing a costume.
- **Do not mix paradigms mid-thought.** A pipeline whose stages mutate shared state has the costs of
  both and the benefits of neither.

---

## 7. Design patterns — using them, and missing them

Patterns are **vocabulary, not a goal** (GoF; Fowler). A pattern earns its place by removing a
specific pain, so any proposal must name the smell it removes and sketch the before/after shape. A
pattern applied to code that is already simple is Speculative Generality with a respectable name.
Norvig's observation is worth holding: many patterns are workarounds for a missing language feature,
so check whether the language already offers the thing before building the pattern.

**The absence of a pattern is equally a finding**, and this is the half usually missed:

- A type-tag `if/else` or `switch` on kind, repeated across several classes → polymorphism, Strategy,
  or a dispatch table. (Fowler: Switch Statements.)
- A chain of "if this handler can't cope, try the next" → Chain of Responsibility.
- Construction logic duplicated across call sites → Factory.
- An interface the caller cannot use directly → Adapter; a subsystem exposing its internals → Facade.
- Behaviour bolted on with flags → Decorator.
- Steps fixed, one varying → Template Method — but check first that the *skeleton itself* should
  exist (see §4; sharing a procedure skeleton is the classic wrong abstraction).
- State-dependent behaviour behind boolean fields → State.

---

## 8. Imperative versus declarative

**Express what, not step-by-step how.** Fowler's argument for collection pipelines (*Refactoring with
Loops and Collection Pipelines*) is that the flow of logic becomes visible and reads close to how you
would state the outcome, and that extracting a loop into a pipeline routinely surfaces business logic
the control flow was hiding. He is explicit that performance should not drive the choice unless a
real, measured problem exists.

His cautions matter too: side effects inside a pipeline destroy its clarity advantage, and some
conditional logic reads worse as a pipeline than as a loop with extracted helpers. Declarative is the
default, not a law.

**The same principle crosses tiers, and that is where the money is.** A transform the datastore,
framework, or library can express declaratively should not be re-expressed as an application loop.
Reading a collection into application memory to change each row and write it back — the SQL world's
"RBAR", row by agonising row — costs round trips, memory, and a hand-written reimplementation of
something already tested, and it usually drags a supporting cast of code along with it. Set-based and
pipeline operations at the tier that owns the data are the declarative form here.

Related: hand-rolling what the platform provides (path traversal, date parsing, JSON walking,
pagination) is the same mistake in a different costume.

---

## 9. Unnecessary complexity

Beck's **four rules of simple design**, in priority order: passes its tests; reveals intention; no
duplication; fewest elements. Note the order — intention-revealing beats de-duplicated, which is why
§4 works.

Specific shapes to design out:

- Nested conditionals beyond two levels → guard clauses and early return.
- A boolean expression that needs a comment to parse → a named predicate.
- **Flag arguments** — a boolean parameter that switches behaviour is two functions sharing one name.
- Temporal coupling — methods that must be called in an order the type system does not enforce.
- Mutable state shared across a boundary for convenience.
- Cleverness. Kernighan's rule: debugging is twice as hard as writing, so code as cleverly as you
  can and you are by definition unable to debug it.

---

## 10. Unnecessary verbosity

Ceremony is not rigour. Code volume is itself a source of complexity, so the same behaviour in fewer
elements is usually better — provided intention survives.

- Boilerplate a language feature removes: records over hand-written data classes, pattern matching
  over cast-and-check ladders, `with`/try-with-resources over manual cleanup, comprehensions over
  accumulate-into-an-empty-collection.
- Defensive code for states the caller cannot produce. A null check on a value the only caller
  constructs, a null-coalesce on a field the schema requires, a try/except around code that cannot
  raise — each teaches the next reader that the impossible is possible.
- Two helpers with different names and identical bodies. Two names imply two behaviours, so the next
  reader must diff them character by character.
- Comments restating the code, and comments as deodorant (Fowler) — a comment explaining a confusing
  block is a request to fix the block.
- Configuration, indirection, and parameters with exactly one value in production.

---

## 11. Idiomatic code — work with the grain

Non-idiomatic code taxes every future reader, and the linter almost never catches it. Use the newest
clean construct the language and version in hand actually offers, and drop the outdated equivalent.
The mistake is nearly always that the developer reached for the construct they knew rather than the
one the platform expects.

The operational test is not "which construct is newer" but "would a fluent practitioner in this
language have reached for this?" The language-specific guidance loaded alongside this charter carries
the per-language detail — use it rather than reasoning from first principles about idiom, and treat a
departure from it as a design finding only when the non-idiomatic form actually costs something: a
type erased, a pipeline turned into an accumulation loop, a platform capability reimplemented, a
resource left to manual cleanup.

---

## 12. What none of this licenses

Breadth here is for *finding* the real problem, not for raising more comments. Still out of scope:

- Nits and bikeshedding — formatting, casing, import order, magic numbers in isolation, log wording.
  The formatter and linter own these, and spending a review on them is a cost with no return.
- Performance speculation with no path to a bottleneck, and premature scale worry about bounded data.
- Patterns, types, or abstractions proposed for a problem that has not appeared.
- Rewrites of code the change did not touch.
- A principle cited without a cost. Naming a principle is how you explain a cost; it is not a
  substitute for having one.

---

## The design review checklist

If nothing else in this document is recalled, recall these. They are the operational lens; everything
above exists to justify and explain them. Work down the list — it is ordered by how much a wrong
answer costs.

1. **Does this need to exist at all?** Could the requirement be met by deleting, configuring, or using
   something that already exists?
2. **Is this complexity essential or accidental?** What would the simplest design that satisfies the
   requirement look like, and what does this one add on top?
3. **What secret does each abstraction hide?** If its implementation changed tomorrow, how many callers
   change?
4. **Is any abstraction sharing shape rather than knowledge?** Would the sites it unifies *have to*
   change together, or do they merely resemble each other today?
5. **Does each module have one reason to change, and do these things belong together?** Name the next
   expected change and count the files it touches.
6. **Is responsibility crossing a seam it shouldn't?** Transport deciding policy, domain knowing
   storage, a client handing its wire format to every caller.
7. **Do dependencies point toward the stable?** Does high-level policy depend on low-level detail, or
   business logic on infrastructure?
8. **Is complexity pushed down into the module, or up onto its callers?** Who is absorbing the
   difficulty — the implementer or everyone else?
9. **Can this module be understood in isolation?** How much other code must be read first?
10. **What does each layer of indirection buy today?** If nothing, why is it there?
11. **Are the domain's concepts modelled as types?** Which bare strings, numbers, or maps are standing
    in for something the business has a name for?
12. **Is behaviour on the object that owns the data**, or drained into a service that reads, decides,
    and writes back?
13. **Is the work expressed declaratively, at the tier that owns the data?** Or re-expressed as an
    application loop over something the platform states directly?
14. **Does this introduce flexibility nobody asked for?** Interfaces with one implementation, config
    with one value, parameters with one caller.
15. **Would this be the same behaviour in fewer elements** without losing intention?
