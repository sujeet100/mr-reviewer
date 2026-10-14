## LANGUAGE-SPECIFIC GUIDANCE (Java 17+ / Spring Boot)

The clean-code charter holds the generic posture (SRP, naming, complexity, DRY, anemic domain, feature envy, design patterns, false-positive guards). This file adds **only what is specific to Java and Spring** — language gotchas, framework patterns, and library-specific traps that change how a generic principle plays out.

### Accuracy notes (do NOT make these false claims at any stage)

- **Java streams are lazy.** Chained `.filter().map().filter()` does NOT iterate the data multiple times — intermediate ops fuse into a single pass during the terminal op. Do not flag chained streams as a performance issue.
- **`Optional` is for return types, not parameters or fields.** Optional on parameters is an anti-pattern; flag it, do not endorse it.
- **Spring beans are singletons by default.** Mutable instance state is therefore shared — but stateless `@Service` classes are the norm. Do not flag stateless beans as "thread-unsafe."
- **`@Transactional` works via Spring proxies.** It does NOT apply to self-invocation, private methods, or methods called by another method of the same class. Flag those, but do not flag normal cross-bean `@Transactional` use.
- **`var` (local variable type inference)** is fine in idiomatic places (`var users = repo.findAll();`). Do not flag it as harmful per se — flag only when it obscures the type at a complex expression.
- **Records give you `equals`/`hashCode`/`toString` and immutability of the fields** — but a record containing mutable collections is not deeply immutable. Be precise.
- **`@Data` on JPA entities breaks `equals`/`hashCode`.** Field-based equality combined with lazy-loaded proxies and generated IDs creates bugs. Flag.

---

### ARCHITECT-stage items (Java/Spring shape)

The charter covers anemic domain and feature envy in generic terms. The Java specialisation:

**Where Spring services drift anemic** — Java is the textbook setup for this:
- `entity.getX()` → process → `entity.setX(...)` in a service method = anemic. Move to `entity.doSomething(...)`.
- Multi-setter state transitions in a service (`order.setStatus(SHIPPED); order.setShippedAt(now()); order.setTracking(t);`) → `order.ship(t)` so the transition is atomic and validated.
- Validation in services where it belongs in the constructor or factory method of the entity.
- Business rules named like `canUserPurchase(user, product)` in a service → `user.canPurchase(product)` or `product.canBePurchasedBy(user)`.
- `@Service` classes named `Manager` / `Processor` / `Handler` stuffed with domain logic — entity behaviour wearing a service hat.

What services SHOULD do (recognise overreach by what's left over): transaction management, loading/saving via repositories, publishing domain events, coordinating between aggregates, infrastructure concerns (external APIs, messaging).

**Spring-specific structural smells**
- **Layer violation**: controllers calling repositories directly, skipping the service layer.
- **Circular bean dependencies**: restructure; do not patch with `@Lazy` / setter injection.
- **Cross-cutting concerns duplicated** (security, logging, transactions, retries) → AOP / interceptor / `@Around` aspect.
- **`static` utility classes that hold real behaviour with hidden dependencies** → make it an injectable bean.

**Leaky abstractions characteristic of JPA/Spring**
- Returning the internal `List<X>` directly (callers can mutate) → `List.copyOf(...)` or `Collections.unmodifiableList(...)`.
- `Specification<EntityType>` exposed through the service interface — callers depend on JPA. Use a domain `*SearchCriteria` instead.
- Service throwing `JpaSystemException` / `DataAccessException` to upstream layers — wrap in a domain exception (`UserNotFoundException`, `ConcurrencyException`).
- DTOs that mirror DB columns (`addressFk: Long`, `statusCd: String`, `createdTs: Timestamp`) instead of domain-meaningful fields (`address: AddressDto`, `status: Status`, `createdAt: Instant`).
- `getProductFromCache(...)` / `getProductFromDatabase(...)` — fetch strategy leaking into the caller.

---

### SENIOR DEVELOPER-stage items (Java/Spring bugs)

The charter covers vague names, overcomplicated logic, "bites you later" generically. The Java specialisation focuses on bug classes the language and framework introduce.

**`Optional` misuse**
- `optional.get()` without `isPresent()` — use `.orElseThrow(...)`, `.orElse(...)`, `.ifPresent(...)`, or `.map(...)`.
- `Optional.of(null)` — throws NPE on construction. Use `Optional.ofNullable(...)`.
- `Optional<List<X>>` / `Optional<Map<K,V>>` — return an empty collection instead.
- `Optional` as a parameter or field — anti-pattern.

**`equals` / `hashCode` (JPA bug class)**
- Field-based `equals` / `hashCode` on JPA entities using a generated ID breaks across transient/detached/Set membership. Use a stable business key, or override only `equals` for entity identity and `hashCode` to return a constant — and document why.
- `@EqualsAndHashCode` and `@Data` on JPA entities — both are landmines.
- `hashCode` without `equals` (or vice versa) — broken by definition.
- `equals` taking a more specific type than `Object` — that's an overload, not `Object.equals`.

**Comparison and equality bugs**
- `==` on `String`, wrapper types (`Integer`, `Long`), enums-with-values — should be `.equals()`.
- `x.equals(literal)` is NPE-prone if `x` is null. Prefer `"literal".equals(x)` or `Objects.equals(x, literal)`.

**Generics and type safety**
- Raw types: `List` instead of `List<String>`.
- Unchecked casts that can throw `ClassCastException` — suggest `instanceof` pattern or a typed alternative.

**Resource management**
- Try-with-resources for every `AutoCloseable`. Flag manual `try { ... } finally { x.close(); }`.
- Connection / session / channel leaks.

**Spring proxy gotchas (the real Spring bugs)**
- `@Transactional` / `@Async` on `private` methods or self-invoked methods — proxy does not apply.
- Double-checked locking without `volatile` on the field — broken.
- Synchronising on non-final fields, `String` literals, or boxed primitives — broken primitives.
- Mutable static fields or static collections in a Spring bean — actual shared mutable state across threads. Flag with the specific failure scenario.

**JPA / Spring Data pitfalls**
- **N+1 queries**: `findAll()` followed by a loop walking a `@ManyToOne` / `@OneToMany` lazy association. Suggest `@EntityGraph`, fetch joins, or `Hibernate.initialize`.
- `@OneToMany(fetch = FetchType.EAGER)` — almost always a mistake; explain why entity-level eager fetch hurts.
- Saving inside a loop without batching.
- `@Transactional(readOnly = true)` missing on query methods that don't write — perf hint, not critical.
- `EntityManager.merge` used when a fresh fetch is intended — different semantics.

**Spring patterns**
- **Field injection (`@Autowired` on fields)** — prefer constructor injection. Constructor injection enables `final`, easier testing, makes circular deps explicit at startup.
- `@Component` / `@Service` doing work in a constructor — should be in `@PostConstruct`, or lazy.
- Blocking calls (`RestTemplate`, sync JDBC) inside a `WebFlux` reactive pipeline.

**Exception handling — real bugs only** (skip style)
- Truly swallowed exceptions: empty `catch`, `catch { return null; }`, `catch { /* ignore */ }` with no log, no rethrow, no recovery.
- `catch (NullPointerException e)` as a substitute for fixing the null source.
- `catch (InterruptedException e)` without `Thread.currentThread().interrupt()` or rethrow — silently breaks cancellation.
- Lost cause chain — `throw new MyError("...")` inside a catch dropping the original `e`. Use `new MyError("...", e)`.
- Catching `Throwable` / `Error`.

Do **not** flag `catch (Exception e)` at outer boundary handlers, log-and-throw preference, `e.printStackTrace` (linter handles it), or "should be more specific" when the catch is already at the right boundary.

**Security — concrete attack path only**
- Hardcoded credentials / API keys / tokens / connection strings.
- SQL injection: string concatenation of user input into JPA `@Query` or JDBC.
- Deserialisation of untrusted data (Jackson polymorphic, ObjectInputStream on attacker-controlled bytes).
- Sensitive data (passwords, tokens, full PAN) in logs.
- Missing auth on a brand-new endpoint that should clearly have one.

Do **not** flag speculative "could be exploited if user input were...", weak crypto on non-security checksums, `SecureRandom`-vs-`Random` for non-security IDs, or TLS speculation on internal calls.

**Performance (obvious wins only)**
- `List.contains` in a hot loop where the collection should be a `Set`.
- Object creation in tight loops that could be hoisted (especially `Pattern.compile`, `ObjectMapper`, `DateTimeFormatter`).
- Stream stored in a field for reuse — streams are one-shot.
- Parallel streams on small collections — adds overhead without benefit.
- Autoboxing in tight loops where `IntStream` / `LongStream` applies.

**Modern Java (suggest, don't dogmatise)**
- `record` instead of a POJO with only constructor + getters.
- Switch expressions over switch statements when each arm yields a value.
- Pattern matching for `instanceof` (`if (x instanceof User u) { ... }`).
- Sealed classes / interfaces for restricted hierarchies (matches OCP nicely).
- Text blocks for multi-line strings.
- `Map.of`, `List.of`, `Set.of` for immutable literals.
- Stream API where idiomatic — but reject "stream-ify a 3-line loop just because."

**REST API design**
- HTTP semantics: GET retrieves, POST creates, PUT replaces, PATCH partially updates, DELETE removes.
- Resource URIs are plural nouns; verbs in URIs (`/getUser`) → `/users/{id}`.
- Status codes: 201 + `Location` for create, 204 for no-content, 400 for validation, 404 for missing, 409 for conflict, 422 for unprocessable.
- Pagination via `page` / `size` / `sort`, with total / links in the response shape.
- Request validation via Bean Validation (`@Valid`, `@NotNull`, `@Size`, `@Email`).
- DTOs in / DTOs out — never expose JPA entities directly.
- API documentation via **OpenAPI annotations** (`@Operation`, `@ApiResponse`, `@Schema`, `@Parameter`) — NOT JavaDoc. JavaDoc is internal; OpenAPI generates consumer-facing docs.

**Configuration**
- Externalise environment-specific values; flag hardcoded URLs / credentials / feature flags.
- `@ConfigurationProperties` with `@Validated` and `@NotNull` constraints.
- Spring profiles for environment-specific behaviour — not `if (env.equals("prod"))` inside code.
- Property naming: kebab-case in YAML (`my-app.feature-enabled`).

**Tests — Java specialisation** (charter covers generic test quality)
- **AssertJ over JUnit assertions.** Materially changes failure diagnostics in CI.
  ```java
  // Flag
  assertEquals(expected, actual);
  assertTrue(result.isPresent());
  assertThrows(IllegalArgumentException.class, () -> service.process(null));

  // Prefer
  assertThat(actual).isEqualTo(expected);
  assertThat(result).isPresent().hasValue(expected);
  assertThatThrownBy(() -> service.process(null)).isInstanceOf(IllegalArgumentException.class);
  ```
  Use AssertJ's matchers (`.containsExactly`, `.extracting`, `.usingRecursiveComparison`).
- **Parameterised tests** when 3+ tests differ only in input/output — `@ParameterizedTest` + `@MethodSource` / `@CsvSource`.
- **Test Data Builders / Object Mothers** when complex objects are constructed inline across tests.
- **Mock at the boundary**, not value objects. Mocking a DTO / record / enum is a smell.
