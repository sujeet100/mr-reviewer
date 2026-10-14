## LANGUAGE-SPECIFIC GUIDANCE (Python 3.10+)

The clean-code charter holds the generic posture (SRP, naming, complexity, DRY, anemic models, false-positive guards). This file adds **only what is specific to Python** — language gotchas, type-system traps, and library-specific patterns. Items unique to LLM / agent code are at the bottom.

**The baseline is 3.10.** Anything newer carries its version inline (`3.12+`), and a feature the project's own floor cannot run is a wrong finding rather than a style preference — check `pyproject.toml` / `setup.cfg` before suggesting one.

### Accuracy notes (do NOT make these false claims at any stage)

- Generators / `map` / `filter` / generator expressions are lazy. Chaining them does NOT iterate multiple times — they fuse under the terminal consumer.
- `dict` iteration order is guaranteed since Python 3.7. Do not flag insertion-order reliance.
- f-strings are faster than `.format()` and `%` — do not suggest switching for performance.
- `list.append` is O(1) amortized.
- On a standard GIL-enabled CPython build, threads give concurrency for I/O and not parallelism for CPU — suggest `multiprocessing` or a native extension. Do not state it as a law: free-threaded builds (3.13+) and C extensions that release the GIL both break it, so name the build if the finding depends on it.
- `asyncio` is cooperative, not preemptive. Do not flag `await` placement as a race-condition risk unless there is genuinely shared mutable state between tasks.
- `is None` / `is not None` is correct — never flag it. Flag `== None`.
- `@dataclass(slots=True)` prevents adding new attributes — do not suggest dynamic attribute additions on a slots dataclass.
- **Raising inside `except` does not lose the original exception.** Python sets `__context__` implicitly and the traceback still shows it. `raise ... from e` sets `__cause__` and changes the wording from "during handling of the above" to "direct cause" — worth asking for when the causality is the point, never reportable as a lost traceback.
- `temperature=0` does not make LLM output deterministic. Never promise reproducibility from it.

---

### ARCHITECT-stage items (Python shape)

**Module / package structure**
- Module-level mutable globals — unsafe across threads / tasks and untestable. Flag.
- Circular imports → boundary problem; the cure is moving code, not adding lazy imports.
- `import *` in production code.

**Separation of logic from I/O (Python idiom)**
- Pure computation in one function, I/O (DB, network, file, LLM call) in another. Flag functions that fetch + transform + save in one body — three concerns crammed together. (This is the charter's "behaviour lives with data" applied through Python's preferred pure-core / imperative-shell split.)

**Class / data shape choices**
- Classes where every method is a getter/setter — use a `@dataclass` (or `@dataclass(frozen=True, slots=True)` for value objects).
- API / JSON models with validation → **Pydantic** `BaseModel`, not hand-rolled.
- `__slots__` for high-instance-count classes — memory win.
- Functions returning raw ORM objects to callers that only need a few fields → map to a DTO / dataclass at the boundary.

---

### SENIOR DEVELOPER-stage items (Python bugs and traps)

**Type hints (treat absence on new code as a defect)**
- Every new function signature needs types — params and return. Untyped return leaks `Any` through callers.
- `from __future__ import annotations` at module top — lazy forward references. (On 3.14+ deferred evaluation is the default under PEP 649, so do not ask for it there.)
- Modern syntax: `X | Y` over `Union[X, Y]`; `X | None` over `Optional[X]`; built-in generics (`list[int]`, `dict[str, int]`) over `typing.List` / `Dict`; PEP 695 `type` aliases; PEP 695 generic syntax (`def first[T](xs: list[T]) -> T`) over explicit `TypeVar`.
- Precision: `Literal[...]` for restricted strings; `Final` for module-level constants; `Self` (3.11+) in methods returning the instance; `@override` (3.12+) on overrides; `NewType` for identifier-like primitives; `TypedDict` over loose `dict[str, Any]`; `Protocol` for duck-typed interfaces; `assert_never` in the final branch of an exhaustive match, so a new variant fails the type check instead of falling through; `TypeIs` (3.13+) for a narrowing predicate the checker actually understands.
- **Accept the weakest capability that does the job; return the concrete one.** `Iterable[T]` when the body only iterates, `Collection[T]` when it needs `len` or `in`, `Sequence[T]` when it indexes or traverses twice, `Mapping[K, V]` for read-only lookup. `list` and `dict` are invariant, so demanding them in a parameter position rejects compatible callers for nothing. Return types go the other way — a concrete `list[Item]` the caller owns beats an `Iterable` whose replayability they have to guess.
- Runtime validation: type hints are not enforced. For external input (APIs, config, user data) use Pydantic / attrs / dataclasses-with-validators. Flag code that trusts unvalidated input to match its hints.

**`Any`, `object` and `cast` — where the type system leaks**

- **Track propagation, not instances.** One `Any` at a boundary erases everything downstream of
  it: `def parse(raw: Any) -> dict[str, Any]` feeding `def process(d: dict[str, Any]) -> Any`
  feeding `def save(result: Any)` is three typed-looking signatures checking nothing. The finding
  is the chain, not the first annotation — contain `Any` at the boundary and convert immediately
  into a typed model.
- **`object` is not a polite `Any`.** It is right where a value genuinely may be anything and the
  code makes no unchecked assumption about it (`def stable_repr(value: object) -> str`), and wrong
  as a way to dodge modelling a shape the program already knows — `state: dict[str, object]` over a
  stable schema is a dataclass or `TypedDict` in disguise.
- **`cast` as a silencer.** Flag `cast()` whose purpose is to stop the checker complaining; the
  honest fixes are `isinstance`, `TypeIs`, validation, a discriminated union, `match`, or a better
  signature. It is legitimate only at an untyped third-party boundary where the invariant is real
  but inexpressible, and there the invariant should be visible in the surrounding lines.

**TypedDict — safe consumption patterns**

Defining a TypedDict is the easy part. Most type erosion happens at the use site. Flag:

- **`.get("required_key")` on a `Required` key** — `.get()` widens to `X | None`. Use `state["key"]` for required keys; reserve `.get()` for `NotRequired` keys.
- **`{**state, "x": 1}` returns `dict[str, object]`, not the TypedDict** — schema lost. In LangGraph (or similar reducer-based) workflows, a node should return only the partial update (`return {"order": order}`), letting the reducer merge.
- **Helpers typed `dict[str, Any]` that accept TypedDicts** — every caller's shape is erased on entry. Type the parameter as the specific TypedDict (or a Protocol / TypedDict union).
- **`cast(X, state["key"])`** — needless if `state["key"]` is already typed; if the runtime type is narrower, narrow with `isinstance` or an early-return guard. `cast` is a runtime lie.
- **`state.items()` / generic iteration** — values typed `object` because TypedDict is heterogeneous. If you're iterating fields, you almost certainly want a dataclass or Pydantic model.
- **`dict(state)`** strips the TypedDict shape downstream.
- **`**kwargs: Any` mirroring a TypedDict** → `**kwargs: Unpack[TD]` (PEP 692, 3.12+) for end-to-end checking.

**Dynamic attribute access (type erasure)**

`getattr` / `setattr` / `hasattr` with a string name return `Any`, are invisible to mypy, don't survive renames, and break IDE navigation.

- `hasattr(model, "field")` on a Pydantic model or dataclass — the field is declared, `hasattr` always True. Use `model.field is not None` if the intent is "value is set." `hasattr(model, "field") and model.field` is a near-certain bug pattern.
- `hasattr(x, "variant_attr")` to narrow a union — use `isinstance(x, Variant)`; mypy narrows after `isinstance`, not after `hasattr`.
- `getattr(obj, "field_name", default)` walking attribute names in a loop — design wants `model.model_fields` (Pydantic), `dataclasses.fields()`, polymorphism, or a dispatch table.
- `setattr(model, "field", value)` — prefer direct assignment or `model.model_copy(update={"field": value})` on Pydantic.

Legitimate uses (do NOT flag): plugin / registry dispatch from config; optional-method protocols (`if hasattr(agent, "cleanup"): agent.cleanup()`); `monkeypatch.setattr` in tests.

**Correctness bugs unique to Python**
- **Mutable default arguments**: `def f(x=[])` / `def f(x={})` — defaults shared across calls. Use `None` and initialise inside.
- **Late-binding closures**: `lambdas = [lambda: i for i in range(3)]` all capture the final `i`. Use the default-argument trick or `functools.partial`.
- **Iterator exhaustion**: assuming a `map` / `filter` / `zip` / generator can be iterated twice.
- **`dict.keys()` where `dict` suffices**: `if k in d.keys()` → `if k in d`.
- **String `+` in loops** → O(n²). Use `"".join(parts)`.
- **`zip(a, b)` without `strict=True`** when lengths must match — silent truncation is a bug class.

**Null safety**
- `X | None` return types used without a guard. Prefer early return / raise on `None` when `None` is a logical error.
- Avoid `Optional` parameters defaulted to `None` that are immediately overwritten — use required arguments or overloads.
- **`None` means absence, not "argument omitted".** Where `None` is itself a valid value for a parameter, defaulting to it makes the two cases indistinguishable. Use a module-level sentinel (`_UNSET = object()`, or a private enum member when it must be typed) so "not provided" and "explicitly `None`" stay separable.

**Error handling — real bugs only** (skip style)
- **Bare `except:`** — catches `KeyboardInterrupt` and `SystemExit`. Always wrong.
- **Truly swallowed exceptions** — `except Exception: pass` with no log, no reraise, no recovery.
- **`except Exception` deep in business logic** (not at outer boundary) — flag only when you can name the specific narrower exception that should have been caught.
- **A `try` covering more than the operation its handler is for.** Five statements under one `except KeyError` means an unrelated lookup three lines down is silently handled as the expected failure. Narrow the block, not the handler.
- **`assert` doing untrusted validation.** `python -O` strips assertions, so an `assert` guarding external input is a check that disappears in the deployment most likely to need it. Raise a real validation or domain error, and keep `assert` for invariants that are impossible unless the program is already broken.
- **EAFP where a pre-check only duplicates the operation.** `if key in d: return d[key]` does the lookup twice and races anything concurrent, where `try: return d[key]` / `except KeyError:` states the intent once. Suggest it there, not as a blanket rewrite of every guarded access.

Do **not** flag: `except Exception` at outer request/worker boundaries, `log.exception` vs `log.error(str(e))` preference, `try/except/else/finally` shape choices, or a missing `from e` as a "lost traceback" — the context is preserved either way (see the accuracy notes).

**Resource management**
- Any resource needing cleanup (files, locks, DB connections, HTTP sessions) must use `with`. Flag manual `open()` / `close()` pairs.
- A group of resources whose membership is only known at runtime → `contextlib.ExitStack` / `AsyncExitStack`, rather than a hand-rolled try/finally chain that leaks whichever one opened before the failure.

**Concurrency — frameworks handle most of this**

Most Django / FastAPI / Flask views are stateless and have no thread-safety story to review. Do not flag stateless code as "thread-unsafe." Only flag with a **concrete failure scenario**:

- `time.sleep` in `async def` — blocks the event loop. Use `await asyncio.sleep`.
- Blocking I/O inside `async def` (sync `requests`, sync DB drivers) — wrap with `asyncio.to_thread` or use an async client.
- Mutable state shared across threads / tasks with no lock — a module-level dict / list mutated from request handlers.
- **`asyncio.create_task(...)` with the result discarded.** The loop keeps only a weak reference, so a fire-and-forget task can be garbage-collected mid-flight and its exception never retrieved — the work half-happens and nothing reports it. Retain the task, or own it with a `TaskGroup`.
- **Swallowed cancellation.** `except asyncio.CancelledError: pass`, or a bare `except Exception` around an `await` that catches it, turns a shutdown request into a hang. Cancellation is control flow: clean up, then let it propagate.
- `asyncio.gather` where one failure should cancel siblings — `TaskGroup` is the right primitive (only when cancel-on-fail matters).

Do **not** flag `ThreadPoolExecutor` vs `ProcessPoolExecutor` choice without a specific failure case, or method-local variables as "thread-unsafe."

**Security — concrete attack path only**
- Hardcoded credentials / API keys / tokens.
- `eval` / `exec` / `pickle.loads` on untrusted input — RCE.
- `subprocess(..., shell=True)` with user-controlled string.
- f-string SQL with user input.
- PII / tokens in logs.
- `requests.get(...)` without `timeout=` when the call is in a request path.

Do **not** flag `secrets` vs `random` for non-security IDs (correlation IDs, retry jitter), speculative shell injection on constants, or "could be SSRF" without evidence the URL is user-controlled.

**Python idioms (suggest, don't dogmatise)**
- `pathlib.Path` over `os.path`; `.read_text()` / `.write_text()` over manual `open` for simple cases.
- `enumerate(xs)` over `range(len(xs))`.
- `itertools.pairwise`, `itertools.batched` (3.12+), `itertools.chain.from_iterable` over hand-rolled loops.
- Comprehensions for building collections; never for side effects.
- Generator expressions when you don't need the full list (memory).
- `collections.defaultdict`, `collections.Counter` over hand-rolled equivalents.
- `dict.get(key, default)` over `if key in d: ... else: ...`.
- `match` / `case` (3.10+) over long `if`/`elif` chains on type / shape / value.
- `functools.lru_cache` on **functions**, not methods (leaks `self`).
- `operator.itemgetter` / `attrgetter` over lambdas in `sorted` / `max`.
- Manual bookkeeping — `found = False`, `count = 0`, `total = 0`, `result = []` threaded through a loop — where `any`, `all`, `sum`, `next`, `min`, `max` or a comprehension states the intent in one line.
- `any(x.ok for x in xs)` over `any([x.ok for x in xs])` — the list form materialises everything and defeats the short-circuit.

The pressure runs both ways, and this is where a Python reviewer most often becomes a nuisance. Do **not** suggest a comprehension where the loop carries nested iteration, several conditions, side effects, exception handling, or state the reader has to unpick — one that needs a comment to decipher is worse than the loop it replaced. Nor suggest a generator where values are reused, indexed, or the list is part of the contract.

**Configuration**
- Externalise env-specific values; flag hardcoded URLs / creds / flags.
- Use `pydantic-settings` / `dynaconf` / typed env loaders — not scattered `os.environ.get`.
- Validate config at startup.
- `os.environ["KEY"]` at module-import time makes the module unimportable without the var. Load lazily.

**Tests — Python specialisation** (charter covers generic test quality)
- **`pytest` over `unittest`** for new code — fixtures, parametrize, plain `assert`.
- **`@pytest.mark.parametrize` with `ids=[...]`** when 3+ tests differ only in input/output.
- **Factories** (`a_user(...)`, `an_order(...)`) or `@pytest.fixture` for complex objects repeated across tests; `factory_boy` or a Test Data Builder for heavier objects.
- **Mock at the boundary** (the module where the name is *looked up*, not where it's defined).
- **`responses` / `respx` / `aioresponses`** for HTTP — beats generic mocks.
- **`pytest.raises(SpecificError, match=r"...")`** — assert both type and message.
- **`tmp_path`** for filesystem tests — never hardcoded `/tmp`.
- **`monkeypatch`** for env vars and attribute patching over manual setup/teardown.
- **`hypothesis`** for pure functions with a clear contract.

---

### LLM / agentic code (LangChain, LangGraph, custom agents)

**LLM-driven code is non-deterministic, expensive, and a prime target for prompt injection.** Apply these in addition to the general items above.

**ARCHITECT stage**

- **Use LCEL** with `|` pipes over deprecated `LLMChain` / `ConversationChain` / `SequentialChain`. Flag new code using legacy chains.
- **Split packages**: import from `langchain-core`, `langchain-openai`, `langchain-anthropic` — not the monolithic `langchain` package when a split exists.
- **Typed LangGraph state**: `TypedDict` (or Pydantic) with `Annotated[...]` reducers where fields merge (`Annotated[list[BaseMessage], add_messages]`). Flag plain `dict` state or missing reducers.
- **Node purity**: a node takes state, returns a partial update. Flag nodes that mutate globals, call external services without error handling, or trigger unbounded recursion.
- **Conditional edges via `add_conditional_edges`** with a routing function — flag long `if/elif` inside one node when branching is the real pattern.
- **Persistence / checkpoints** (`MemorySaver`, `SqliteSaver`, `PostgresSaver`) where the product semantics need durable state — resume, human-in-the-loop, long-running workflows, crash recovery, durable thread state. Do **not** flag a one-shot stateless workflow merely for lacking a checkpointer.
- **Memory vs state**: separate short-term (conversation) from long-term (vector store / DB). Flag everything-in-one-list-forever.
- **Tool registry centralised** with typed schemas. Flag tools defined inline in multiple chains — schema/description drifts.
- **Business rules embedded in prompts** — untestable, unversionable. Keep reasoning, tool execution, and business rules separate.
- **RAG**: semantic / structural chunking when available; metadata filtering not embedding-similarity alone; reranking for precision-sensitive queries; source attribution on every answer.

**SENIOR DEVELOPER stage**

- **Prompt injection**: never `.format()` / f-string user content into system prompts. Use message roles and delimiters. Flag user input concatenated into instructions.
- **Tool input validation**: tool args come from the LLM — validate against Pydantic before executing. Flag tools that pass LLM-generated strings to `subprocess` / `eval` / SQL / shell / filesystem.
- **Destructive tool gating**: file-delete / DB-write / payments / email-send / PR-merge / production-API tools need (a) HITL interrupt, (b) policy check, or (c) dry-run mode.
- **Output validation**: validate structured LLM output against schema AND domain rules. Flag code that trusts the LLM verbatim (e.g. "pay this amount" → `charge(amount)` without bounds check).
- **Allowlists over denylists** for tool selection / URL access / file paths.
- **Rate limit + cost cap**: every agent loop needs max-iteration cap + token / cost budget. Flag loops that can spin forever.
- **Secrets**: never in prompts or tool-call arguments.
- **PII / PHI**: do not pass raw to the LLM unless the service is compliant.
- **Model pinning**: pin specific model versions (`claude-sonnet-4-5-20250929`) in production paths — not aliases that silently roll forward.
- **Determinism**: `temperature=0` narrows variation but does not make output reproducible — batching, routing and floating-point non-associativity all still move it. Ask for determinism from the code around the model (schema validation, bounds checks, golden-dataset evals), never from the sampling parameter.
- **Structured output**: `llm.with_structured_output(PydanticModel)` or tool-calling — flag regex parsing of LLM output.
- **Retries**: `.with_retry(stop_after_attempt=N)` on transient errors in production paths.
- **Fallbacks**: `.with_fallbacks([alt_model])` for resilience in critical paths.
- **LangGraph recursion limit**: pass `config={"recursion_limit": N}` to every run.
- **Agent loop hygiene**: every `(think → act → observe)` loop needs a max-iteration cap, a termination condition other than "LLM said done", and progress detection.
- **Tool description quality**: tool descriptions ARE the prompt — flag vague / missing `description` fields; descriptions must say when to use AND when not to.
- **Token / context bounds**: flag unbounded message-history accumulation; use `trim_messages` / `RunnableWithMessageHistory` / a summariser.
- **Observability**: log prompts, responses, tool calls with a correlation ID and per-call token usage.

**Testing LLM code**
- Mock LLMs with `FakeListChatModel` / `FakeMessagesListChatModel` or VCR cassettes (`vcrpy`). Flag unit tests that call real models.
- Eval suite (golden dataset + grading: exact match, LLM-as-judge, or human) — flag chains/agents shipped with only happy-path unit tests.
- When prompts change, run the eval suite. Flag prompt changes without an eval-run note in the MR.
- Tool-call testing: assert the agent picks the right tool with right args, not just final output.
- Cost / token assertions for cost-sensitive agents.
