# LENS — concurrency

Read this because the code you are reviewing involves threads, async execution, scheduling,
shared state or parallel streams. It adds questions to your pass; it does not change your remit.

The bar here is higher than elsewhere, because concurrency is where speculative findings breed.
A finding needs **the shared mutable state, the two actors, and the sequence** in which they
interleave to produce the wrong result. "Could race" without those three is not a finding.

## Ask

- **What is shared, and who mutates it?** A static field, an instance field on a singleton bean, a
  cache, a collection handed out by reference, a closure over a mutable local. If nothing is
  shared and mutable, stop.
- **Is the guard on every path?** A `synchronized` block that protects the write but not the read
  that decides whether to write; a check-then-act (`if (!map.containsKey(k)) map.put(k, v)`) on a
  concurrent map that has `computeIfAbsent`.
- **Does the async boundary drop the context?** Security context, transaction, MDC, request scope:
  which of them does the new thread or task not carry, and what reads it there?
- **Are exceptions in a task observed?** A `CompletableFuture`, an `asyncio.create_task`, an
  `ExecutorService.submit` whose failure nobody joins or awaits fails silently.
- **Is the operation safe to run twice?** Schedulers fire on overlap, consumers redeliver, users
  double-click. Name the second run's effect on the shared state.
- **Blocking inside an async or reactive path.** A synchronous DB or HTTP call on an event loop or
  a reactive pipeline.
- **Is `volatile` or an atomic doing what the author thinks?** `volatile` gives visibility, not
  atomicity of read-modify-write.

## Do not flag

- Thread-safety of stateless, framework-managed components (a Spring `@Service` with no mutable
  fields, an Angular service, a FastAPI handler).
- Method-local variables.
- "Consider using a thread pool" without a demonstrated problem.
- Immutable values shared across threads.
