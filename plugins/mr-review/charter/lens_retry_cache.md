# LENS — retries, caching and repetition

Read this because the code you are reviewing retries, caches, schedules or otherwise runs, or
returns, something more than once. It adds questions to your pass; it does not change your remit.
The common defect is not the mechanism itself but what it assumes about the operation it wraps.

## Ask

- **Is the retried operation idempotent?** A retry around a create, a send, a charge or an
  append that is not idempotent is a duplicate waiting for a timeout. Name the second attempt's
  effect.
- **What is retried, exactly?** The whole method or the one call that can fail transiently? A
  retry around a block that includes a write commits the write again.
- **Which failures are retried?** Retrying a 4xx, a validation error or a parse failure burns the
  budget on something that will never succeed; not retrying a timeout defeats the purpose.
- **Is there a bound?** Attempts, total time, backoff with jitter. `retries=∞` and `timeout=0`
  are the dangerous defaults.
- **What does the cache key contain?** Everything the result depends on — tenant, locale,
  version, the arguments actually used — and nothing that varies without changing the result.
  A key missing the tenant is a cross-tenant leak.
- **When is it invalidated?** Name the write that changes the underlying data and check it
  evicts. A cache that is never invalidated is a stale read with a TTL you have to find.
- **Can a cached call bypass a check?** An authorisation or feature-flag decision computed before
  the cache lookup is fine; one computed inside the cached method is skipped on every hit.
- **Does the framework already do this?** `@Cacheable`, `@Retryable`, `lru_cache`, a client with
  built-in retry: a hand-rolled version next to a platform one is a decision worth naming.
- **Scheduled work.** Overlapping runs, a fixed delay versus a fixed rate, and what happens on the
  first run after a long outage.

## Do not flag

- Memoisation the framework provides (pure pipes, `computed()`, React Compiler).
- Retry policy values without a concrete consequence of the current value.
- A cache on read-only reference data with no writer.
