# CRAFT QUESTIONS — how to read code at function level

`clean_code.md` and `craft_lens.md` are catalogues: they name the smells and tell each one from
its harmless neighbour. This file is how you *find* them. A reviewer who scans a diff against a
catalogue reports what is salient — obvious bugs, big structures, unusual code — and walks past the
subtle findings, because those are judgement calls that need slow local reasoning rather than
pattern matching. The questions below are that reasoning.

**Your primary job is to find code that works but makes the next engineer think harder than
necessary.** Do not spend most of your attention hunting rare runtime bugs; other passes cover
broad correctness, and on a typical change craft problems are far more common. Read each changed
function carefully and ask whether the implementation is the simplest, clearest expression of the
behaviour.

**One measure unifies all of it:** how much must the reader hold in their head at once — state,
control flow, indirection, and domain knowledge they have to go elsewhere to find? Every finding
below is that number being higher than the behaviour requires. Nested loops and mutables that
evolve down a method are too much control flow and state; a dense boolean is too much decoding; a
vague name withholds meaning the reader must reconstruct; one sequence scattered across six tiny
methods is too much navigation.

**These are questions, not a checklist, and never an output format.** Work through the ones the
code actually raises. Do not answer them in a finding, and do not report that you considered one.
Where a question leads somewhere, name the smell from the catalogue, quote the lines, and say what
it costs — the catalogue is how you *explain* a finding, never a list to sweep for matches.

Where a question below says "do not flag", that guard outranks the question.

---

### 1. Is this doing more work than the problem requires?

Are we iterating the same collection several times for no reason? Is there nested iteration where
a lookup, grouping, set, join, database query or existing API would say it directly? Are we
searching a collection inside a loop? Is data transformed several times where one pass would do?
Is control flow compensating for a poor data shape? Is there a simpler algorithm that states the
intent?

Do not optimise for theoretical micro-performance. Flag it when the implementation creates
unnecessary work **or** makes the intent harder to see — usually both at once.

```
for (Order order : orders)                        // O(n x m), plus three levels of conditional
  for (Customer c : customers)                    // state, where the intent is simply "index
    if (c.getId().equals(order.getCustomerId()))  //  active customers by id, then process the
      if (c.isActive())                           //  orders that are not cancelled"
        if (!order.isCancelled()) { ... }
```

### 2. Did you have to run the code in your head?

This is the sharpest question here, and it replaces counting. Pay attention to nested loops,
nested branches, loops combined with branching, mutable variables whose meaning changes down the
method, large boolean expressions, chained transformations, ternaries inside expressions, and state
carried across iterations.

Ask: could you say what this returns without tracing it line by line? How many conditions or
pieces of state did you hold at once to get there? Could the intent be named instead — an
intermediate value, a named predicate, a domain concept that makes the decision obvious?

If understanding required mental execution, that is the finding. **Depth alone will not tell you:**
a three-deep nest that reads as a flat table of rules is fine, and a two-deep one with three
mutables evolving through it is not.

### 3. Is this expression written for the computer or the reader?

For every non-trivial expression: can its meaning be taken in at once? Does it combine several
unrelated decisions? Are precedence rules doing too much work? Would a well-named intermediate
value carry the business meaning? Would extracting a predicate make the rule visible? Is it
cleverer than it needs to be?

Do not prefer shorter code automatically. Prefer the form that communicates intent for the least
effort.

```
return o != null && o.getCustomer() != null && !o.getCustomer().isDisabled()
    && (o.getStatus() == READY || (o.getStatus() == PENDING && o.isRetryAllowed()))
    && !excludedTypes.contains(o.getType());      // parseable; nobody can read the rule
```

### 4. Do the names tell you what the code means?

Look at the variables, collections, booleans, parameters, intermediate values and return values
that matter. Does the name describe the domain meaning rather than the type? Could you tell why
this value exists without reading where it came from? Is `result`, `data`, `item`, `value`,
`info`, `response`, `temp`, `flag`, `type` or `status` hiding a more precise concept? Is a boolean
phrased so that `true` is immediately understandable? Does the name stay accurate for the
variable's whole life?

Comment only where naming genuinely obscures reasoning, never for cosmetic preference, and propose
the better name every time.

### 5. Did decomposition actually help?

Extraction is a means to lower cognitive load, not a virtue, and a function can be over-extracted
as easily as under-extracted. For every small helper this change introduces: does it name a
concept the reader did not already have? Is it reused, or called once? Does the caller now need
*less* context — or does following one flow mean opening three methods? Has a straight-line
sequence been chopped into fragments that only make sense read together? Is a two-to-three line
method merely hiding trivial mechanics behind another name?

`isEligibleForAutomaticRenewal(subscription)` earns its three lines because the name carries a
rule. `getFirstItem(list)` does not. Where extraction added navigation without adding a concept,
say which methods you would merge back and why the name told the reader nothing.

### 6. Has an abstraction introduced here earned its existence?

For an interface, strategy, wrapper, adapter, factory, helper, handler or generic mechanism this
change adds: what concrete problem does it solve, and what becomes simpler because it exists? Are
there genuinely several behaviours behind it? Is it hiding meaningful complexity or merely
relocating it? Would a direct implementation be clearer? Would adding it later, when a second case
appears, be cheap?

Do not reward architectural sophistication. Keep this to what you can see from here — abstractions
spanning files and modules are the architect pass's, not yours.

### 7. Is the control flow expressing the intent?

Deep nesting, several `continue`s or `break`s, `while (true)`, mutable loop-control flags, manual
index manipulation, sentinel values. **Do not flag any of these mechanically.** Ask instead whether
the control flow is the clearest statement of the behaviour, or whether the reader has to
reconstruct the exit conditions and state transitions. Could filtering, mapping, grouping,
matching, an early return, a named operation or a state transition say it better?

Prefer declarative form when it materially improves readability, and leave straightforward
imperative code alone merely because a functional construct exists. This is genuinely clearer than
most pipelines:

```
for (var item : items) {
    if (!eligible(item)) continue;
    process(item);
}
```

### 8. Is data carrying behaviour that belongs to an object?

When code reads values off an object and then decides something about that object elsewhere: who
owns that decision? Is the caller inspecting state and then telling the object what to do? Would
the behaviour be more coherent if the object decided? Is business logic accumulating in services
while the objects stay bags of getters? Would moving it make an invariant enforceable in one place?

Do not push every operation into an entity. Move behaviour when the data and the rules genuinely
belong together.

### 9. Are primitives hiding a domain concept?

Watch for strings, integers, booleans, maps, tuples, ids, status values, currency amounts, dates
and ranges, coordinates, percentages and type codes — especially the same set of values travelling
together. Does the primitive carry domain meaning? Does it have invariants nobody enforces? Is
logic repeatedly interpreting it? Would a small type make an invalid state harder to represent and
simplify the code around it?

Do not wrap every primitive. Introduce a type when meaning, validation or invariants are currently
scattered.

### 10. Is the code simpler after your change?

Before posting, check your own suggestion. Does it reduce the concepts, the nesting, the state the
reader tracks, or the hops between methods? Does it make the domain intent explicit, or an invalid
state harder to represent? If it does none of those and merely swaps one style for another, drop
it — a comment trading an index loop for a comprehension, or the reverse, costs the author time and
buys nothing.
