## LANGUAGE-SPECIFIC GUIDANCE (Angular 17+ / TypeScript)

The clean-code charter holds the generic posture. This file adds **only what is specific to Angular, RxJS, signals, and TypeScript-in-Angular** — framework gotchas, change-detection traps, and reactive composition.

This file is also used for plain TypeScript reviews — skip the Angular-specific items in that case.

### Accuracy notes (do NOT make these false claims at any stage)

- **Signals are synchronous; Observables are async streams.** A signal is not "an Observable without subscribe." Do not flag every signal-vs-observable choice as wrong — they're for different jobs.
- **`takeUntilDestroyed()`** uses the injection context. It is the modern cleanup primitive (Angular 16+); do not prefer `takeUntil(destroy$)` patterns in new code.
- **`@if` / `@for` / `@switch`** are the modern control flow (Angular 17+). Do not flag them in favour of `*ngIf` / `*ngFor`.
- **Standalone components** are the new default. Do not flag standalone as "missing module configuration."
- **`undefined` is the TypeScript convention for "no value."** `null` is acceptable for "explicitly absent" (DB null, API-returned null).
- **`computed()`** and **pure pipes** are memoised — do not flag derived state recomputed via `computed` as wasteful.

---

### ARCHITECT-stage items (Angular shape)

**Where Angular components drift anemic** — the framework pushes thin components and thick services, but the inverse — thick components and thin services — is equally common in this codebase.

- Business logic in components (discount calculation, validation, pricing rules) → service or domain model.
- Data transformation inside `subscribe` callbacks (filter, sort, group by) → push into the service or a mapper.
- State mutation scattered across component methods (push to array, update count, persist to localStorage) → service owns the state machine.
- Validation logic embedded in component (`canSubmitOrder()` computing booleans from local fields) → validation service or domain model method.
- Component classes with 20+ methods of which only a few touch the template — call out the misplaced logic.

What components SHOULD do (recognise overreach by what's left): template event handling, calling services and binding results, component-local UI state (open/closed, selected tab), input/output communication, form setup.

**Smart vs Presentational split**
- Presentational components: only `@Input` / `@Output` (or `input()` / `output()` signals), `OnPush`, no service injection.
- Smart (container) components: data fetching, state, orchestration.
- Flag presentational components that inject services — smart components in disguise.
- Flag smart components with heavy template logic — extract a presentational child.

**Missing / leaky abstractions (Angular-flavoured)**

Missing:
- Components consuming raw API responses (no domain model). Map at the service boundary so the component sees `User { fullName, isActive }`, not `response.data.attributes.first_name`.
- Direct infrastructure access (`localStorage`, `sessionStorage`, `window`, DOM APIs, cookies) inside components → `StorageService` / `PreferencesService`.
- Repeated DTO → view-model mapping inline → a dedicated mapper.
- Multi-service workflow orchestration in a component (load → validate → price → pay → save → clear → notify) → `CheckoutFacade.placeOrder(...)`.
- Third-party library use scattered (`moment`, `lodash`, `date-fns`) → wrap so the library can be replaced in one place.

Leaky:
- `public users$ = new BehaviorSubject<User[]>([])` — consumers can `.next()` and corrupt state. Use `private _users$ = new BehaviorSubject<...>(...); readonly users$ = this._users$.asObservable();` (or `signal<...>().asReadonly()`).
- Service returning `HttpResponse<ApiWrapper<{ items: DataDto[] }>>` — hide HTTP / wrapper details, return `Observable<DomainModel[]>`.
- Component aware of backend URL structure (`${env.cdnUrl}/images/products/${id}/thumb_${imageId}.webp`) → populate `product.thumbnailUrl` in the service.
- Components calling `store.dispatch(...)` / `store.select(...)` directly → wrap in a facade.
- Services exposing raw data structures (`Map<string, { children, parent, data }>`) → behaviour-oriented methods (`getChildren(id)`, `getParent(id)`).

**Service design**
- God services handling multiple unrelated domains → split.
- Circular service dependencies → restructure or mediator; not `@Lazy` / setter injection.
- Service methods mixing fetch + transform + persist + notify → separate or facade.

**State management decisions**

When to suggest a structured layer (signal store, NgRx, NgRx signals, NGXS):
- Multiple components share and modify the same data.
- Prop-drilling across 3+ levels.
- State persists across route navigations.
- Optimistic updates with rollback.
- Time-travel / undo-redo.

When NOT:
- Single-component local state — a signal in the component.
- Read-only data fetched once per route — service with `shareReplay`.
- "Might need it later" — YAGNI.

**Reactive composition (FP at scale)**
- Compose observables with operators rather than nesting subscriptions.
- Derive state with `computed()` signals rather than imperative recompute in lifecycle hooks.
- Pure transformations via pipes; pure components via OnPush + signal inputs.

**Module / feature structure**
- Feature folders over `components/` + `services/` siblings.
- Lazy-loaded feature routes — flag eagerly-imported feature modules in `app.routes.ts`.
- Provider configuration via `provideHttpClient()`, `provideRouter()`, etc. — flag legacy `HttpClientModule` / `RouterModule.forRoot()` in new code.
- Barrel files (`index.ts`) that pull unused exports hurt tree-shaking.

---

### SENIOR DEVELOPER-stage items (Angular bugs)

**TypeScript strict typing**
- `any` — flag every usage. Use `unknown` + type guards, a concrete type, or a generic constraint.
- Implicit `any` from untyped function parameters or returns.
- `any[]` arrays → `unknown[]` with guards, or a real element type.
- Type assertions chained (`x as A as B`) — restructure.
- `@ts-ignore` / `@ts-expect-error` without a comment explaining why.

**Type guards and narrowing**
- Prefer user-defined type guards (`function isUser(obj): obj is User`) over `as Type`.
- Discriminated unions for finite state: `{ status: 'loading' } | { status: 'success', data: T } | { status: 'error', error: Error }`.
- `in` operator and `typeof` for narrowing instead of casting.
- `satisfies` for validating object shapes while preserving literal types.

**Null / undefined handling**
- `??` (nullish coalescing) for defaults, not `||` (`||` also falses out `0` / `''` / `false`).
- `!` non-null assertion — require justification or a proper guard.
- Reflexive `?.?.?.` chains suggest the type isn't right.
- Prefer `NonNullable<T>` over hand-rolled exclusion.

**Generics and utility types**
- `Partial<T>`, `Required<T>`, `Pick<T, K>`, `Omit<T, K>`, `Record<K, V>` over re-typing shapes.
- `Readonly<T>` for immutable structures.
- Over-generic functions with constraints that aren't actually used.
- Excessive `Partial<T>` that makes required fields optional everywhere — defeats the type.

**Enums vs alternatives**
- Prefer `as const` objects or union of string literals over `enum` for string values.
- Numeric enums without explicit values are error-prone.
- `const enum` has bundler issues; flag in shared libraries.

**RxJS — memory leaks and operator choice**
- Subscriptions without cleanup — require `takeUntilDestroyed()` (Angular 16+) or async pipe in templates.
- Subscriptions in constructors (no injection context for `takeUntilDestroyed` at the typical call site) — move to `ngOnInit` or use `inject()`.
- Nested subscriptions (`subscribe` inside `subscribe`) — flatten with `switchMap` / `mergeMap` / `concatMap`.
- Operator misuse:
  - `switchMap` → search / autocomplete (cancels previous).
  - `exhaustMap` → form submission (ignores while processing).
  - `concatMap` → ordered sequential operations.
  - `mergeMap` → independent parallel operations.
- `distinctUntilChanged()` to drop redundant emissions on inputs that often repeat.
- `shareReplay({ bufferSize: 1, refCount: true })` for shared HTTP calls — understand `refCount: true` (unsubscribes when no consumers) vs `false` (caches forever).
- `BehaviorSubject` publicly exposed with `.next()` callable from components — bug.
- `toPromise()` is deprecated — use `firstValueFrom` / `lastValueFrom`.

**Signal misuse**
- Mutating signals via assignment (where API expects `set` / `update`).
- Mixing signals and observables in the same dataflow without `toSignal` / `toObservable` adapters.
- `effect()` used to mutate other signals — Angular will warn.

**Change detection and template performance**
- **Function calls in templates** (`{{ computeTotal() }}`) — called every CD cycle. Move to a `computed()` signal, a property, or a pure pipe.
- Missing `OnPush` on presentational components.
- `ChangeDetectorRef.detectChanges()` calls — usually an architecture problem; explain what's missing rather than suggest more `detectChanges`.
- `markForCheck()` calls — flag and ask why; often a signal would remove the need.
- `track` in `@for` — required, must be meaningful (not `$index` for mutable lists, not a stringified object).
- Complex template expressions — extract to computed signals or properties.
- `loading="lazy"` for below-the-fold images, `@defer` for below-the-fold components.

**Async pipe**
- Multiple async pipes on the same observable in one template → multiple subscriptions. Use `@if (x$ | async; as x)` and reuse.

**HTTP error handling**
- Observable chains without `catchError`.
- `catchError(() => of(null))` silently swallowing errors — ask for real recovery or propagation.
- Missing typed responses (`http.get('/api/users')` instead of `http.get<User[]>('/api/users')`).
- Functional interceptors (`HttpInterceptorFn`) over class-based for new code.
- Cross-cutting concerns (auth, logging, error transformation) duplicated across services — use an interceptor.

**Forms**
- Typed forms: `FormGroup<{ name: FormControl<string> }>` — flag `any` in form types.
- `NonNullableFormBuilder` for required-default forms.
- Validation logic in templates → move to component or a validator function.
- Async validators without debounce — every keystroke hits the server.
- Form recreated on every CD cycle — usually a missing `OnPush` or a misplaced `formBuilder.group()` call.
- `updateOn: 'blur'` / `'submit'` for performance-sensitive forms.

**SSR / hydration**
- Direct DOM access (`document.querySelector`, `window.location`, `localStorage`) without guards (`isPlatformBrowser(this.platformId)`) — breaks SSR.
- `setTimeout` / `setInterval` in components without cleanup — breaks SSR snapshot reproducibility.
- Code depending on browser-only globals in `ngOnInit` — guard or move to `afterNextRender`.

**Security — concrete only**
- API keys / tokens hardcoded in frontend code (always proxy through backend).
- Trusting URL fragments or query params without validation — XSS / open redirect risk.
- `innerHTML` binding (`[innerHTML]`) with untrusted content — XSS. Use `DomSanitizer` and explain why.
- `bypassSecurityTrust*` without a clear comment.
- Logging tokens, PII, or full request bodies on the client.

**Configuration**
- `environment.ts` / `environment.prod.ts` for environment-specific values — flag hardcoded URLs / flags in components.
- Route paths hardcoded as strings throughout the app → route constants.
- Missing config validation at app bootstrap.

**Tests — Angular specialisation** (charter covers generic test quality)
- **Testing Library (`@testing-library/angular`)** over poking the DOM directly.
- **Mocking**: `jest.fn()` / Jasmine spies for service methods; `HttpTestingController` for HTTP. Mock at the boundary, not value types.
- **Async**: `fakeAsync` / `tick` / `flush` for time-based ops; `waitForAsync` for real async. No `setTimeout` in tests.
- **Snapshot tests** only with meaningful assertions.
