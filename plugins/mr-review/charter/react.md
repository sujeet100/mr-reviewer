## LANGUAGE-SPECIFIC GUIDANCE (React 18+ / 19 / TypeScript)

The clean-code charter holds the generic posture. This file adds **only what is specific to React, hooks, and TypeScript-in-React** — bug classes the framework introduces, hook discipline, and rendering pitfalls.

### Accuracy notes (do NOT make these false claims at any stage)

- **React batches state updates** (18+). Multiple `setState` calls in the same handler / effect do not cause multiple renders.
- **`useMemo` / `useCallback` are NOT always beneficial.** The dep-check cost can exceed the work they save. Never blanket-suggest memoisation. If the project uses the React Compiler (React 19), most manual memoisation is dead weight.
- **Effect cleanup runs before re-run AND on unmount.** Both. Do not claim cleanup "only runs on unmount."
- **`react-hooks/exhaustive-deps` is the source of truth** for dependency arrays. Do not claim a dep is missing if it's a stable ref (setter from `useState`, dispatch from `useReducer`).
- **Keys are scoped to siblings** — unique among siblings, not globally.
- **Concurrent rendering ≠ parallel.** React can interrupt rendering but runs on a single thread.
- **`useState(() => expensive())` runs ONCE** — do not claim it runs every render.
- **`undefined` is the TypeScript convention for "no value."** `null` is acceptable for "explicitly absent."

---

### ARCHITECT-stage items (React shape)

**Smart vs presentational split**
- Presentational components: accept props, render UI, no data fetching, no app state. Small, pure, reusable.
- Smart / container components: fetch data, manage state, compose presentational children.
- Flag presentational components with data-fetching hooks — inject via props or context.
- Flag "god components" with 10+ props doing many things — split.

**Missing / leaky abstractions (React-flavoured)**
- Repeated `useEffect(() => { fetch(...) }, [])` for server data → TanStack Query / SWR / RSC. Lacks caching, dedup, retries, race-condition handling, refetch on focus, loading/error states. **Structural choice; flag aggressively.**
- Repeated fetch patterns (same URL shape, same error handling) → custom hook or shared query function.
- Repeated form setups (fields, validation, submit) → custom hook or shared form component.
- Server state stored in Redux / Zustand when a query library would do — server state is not application state.
- Components aware of API URL structure → service / repository layer.
- Components aware of cache / query keys → wrap in a domain hook (`useUser(id)`, not `useQuery(['users', id], ...)`).
- Global store exposed directly to every consumer → selector hooks.
- Context exposing raw setter functions → semantic actions (`addToCart(item)`, not `setCart`).

**Context pitfalls (architectural-scale)**
- A single context storing many unrelated values → every consumer re-renders on any change. Split by update frequency.
- Memoise the context `value` so consumers don't re-render on every parent render.
- For read-only data with many consumers, prefer a selector pattern or a store over raw context.

**State management decisions**
- When to suggest a structured store (Zustand, Jotai, Redux Toolkit): 3+ levels of prop drilling for the same state; state persists across route navigations; many read-only consumers; optimistic updates with rollback.
- When NOT: local component state (use `useState`); server data (use a query library); "might need it later" (YAGNI).

**Composition patterns**
- Prefer `children` prop over render props or complex config.
- Compound components (`<Tabs><Tab/></Tabs>`) for related UI sharing implicit state.
- React favours composition over inheritance — flag any `class extends Component` for code reuse.

**RSC / Server Components (Next.js App Router)**
- Fetch data in server components, not `useEffect` on the client.
- `"use client"` at the lowest possible level. Flag entire pages marked client when only a leaf is interactive.
- Never pass non-serialisable props (functions, `Date`, `Map`, class instances) from server to client without explicit serialisation.

---

### SENIOR DEVELOPER-stage items (React bugs)

**Hooks discipline (CRITICAL — most React bugs hide here)**
- **Rules of Hooks**: top-level only. Never inside conditionals, loops, nested functions, or non-hook utility functions. Flag every violation.
- Hooks only called from React components or other custom hooks. Flag usage in event handlers or general utilities.

**`useEffect` — you probably don't need one**

Each of these is a misuse — flag:
- Computing values from props / state → plain expression or `useMemo`, not an effect.
- Handling user events → put the logic in the event handler.
- Resetting state when a prop changes → `key` prop, or lift state, or derived state.
- Updating parent state based on child state → lift state or callback in handler.
- Effect contains only a state setter → that's derived state; compute inline.

Valid uses (do NOT flag):
- Subscribing to external systems (non-React state, WebSocket, browser APIs).
- Synchronising with the DOM in a way React's render doesn't handle (focus, scroll, third-party widgets).

Effect hygiene:
- Missing cleanup for subscribes / timers / open connections → leak.
- Empty deps `[]` only when the effect is genuinely mount-once.
- Inline object / function in deps → identity changes every render → infinite re-render. Flag.

**`useState`**
- Duplicated state — if it can be derived from other state / props, derive it.
- State that mirrors props — use the prop directly or lift state.
- Multiple related `useState` with complex transitions → `useReducer`.
- Expensive initial value not wrapped in lazy initialiser: `useState(() => expensive())`.
- Reading state immediately after `setState` and expecting the new value — async.

**`useMemo` and `useCallback`**
- Default to NOT memoising. Only memoise when (a) measured performance problem, or (b) the value is a dep of another hook and identity matters.
- `useCallback` wrapping a function not passed to a memoised child or in a dep array — dead weight.
- `useMemo` wrapping a cheap computation (a `.map`, a string concat) — overhead exceeds savings.

**`useRef`**
- For mutable values that don't trigger re-renders and for DOM references.
- `useRef` storing a value that SHOULD trigger re-renders — should be state.
- Reading / writing refs during render — only in effects or handlers.

**State mutation and rendering bugs**
- `items.push(x); setItems(items)` — mutates state, bypasses React's change detection.
- `setX(...)` called during render without a guard → infinite re-render loop.
- Inline object / array / function props to `React.memo`'d children — break memoisation.
- Stale closure in effect / handler — dep missing.
- Event handler registered once in effect referencing later state — use a ref or fresh closure.
- Direct DOM mutation (`document.querySelector`, `innerHTML`) — use refs and declarative state.

**Lists and keys**
- Stable, unique key — never array index unless the list is truly static.
- Keys come from the data, not `Math.random()` / `Date.now()`.
- Key on `<Fragment>` requires explicit `<Fragment key={k}>`, not `<>`.

**TypeScript strict typing**
- `any` — flag every usage. Use `unknown` + narrowing, a concrete type, or generic constraint.
- Type assertions (`as SomeType`) outside narrow escape hatches — prefer type guards or Zod / Valibot validation.
- `!` non-null assertion — require a guard or proper type.
- Every component must have typed props.
- Discriminated unions for props that vary by mode: `{ mode: 'view' } | { mode: 'edit', onSave: () => void }`.
- Don't make every prop optional — required props catch bugs at call sites.
- `children: React.ReactNode` (not `children?:`) when the component is meant to have children.

**Event types**
- `React.ChangeEvent<HTMLInputElement>`, `React.MouseEvent<HTMLButtonElement>`, `React.FormEvent<HTMLFormElement>` — flag `(e: any)`.
- Handler props as function signatures: `onChange: (value: string) => void`, not `React.ChangeEventHandler`.

**Concurrent features (React 18+)**
- `useTransition` for non-urgent updates (filtering a large list while typing).
- `useDeferredValue` to show stale data while new is being prepared.
- `<Suspense>` with libraries that support it.
- `useOptimistic` (React 19) over hand-rolled rollback logic.
- `useActionState` (React 19) for form state tied to server actions.

**Forms**
- `react-hook-form` for non-trivial forms — uncontrolled by default, minimal re-renders, schema validation (Zod / Yup).
- Controlled inputs need `value` AND `onChange` — flag one without the other.
- Mixing controlled / uncontrolled on the same input.
- Validation schemas colocated with the form and reusable server-side when possible.
- Form state scattered across many `useState` calls → form library.

**Accessibility (a11y)**
- Interactive element needs a role and accessible name. `<div onClick>` → `<button>`.
- Form inputs need associated labels (`<label htmlFor>` or `aria-label`).
- Images need `alt` (empty `alt=""` for decorative).
- Modals / dialogs need focus trap and restoration — use `@radix-ui/react-dialog` or `react-aria`, not hand-rolled.
- Semantic HTML first (`<main>`, `<nav>`, `<header>`) before ARIA.
- Click-only interactions (no keyboard support).

**Error handling**
- `fetch` / `await` without try/catch or error UI.
- Error Boundaries for rendering errors (React 19 supports `onError` on function components).
- Silent `catch (e) {}` — show errors to users.
- For data fetching, let the query library surface errors.

**Security — concrete only**
- `dangerouslySetInnerHTML` with user input — XSS. If required, sanitise with DOMPurify and explain why.
- Direct URL construction from user input in `<a href>` / `window.location` — validate protocol (block `javascript:`).
- Sensitive tokens in `localStorage` — prefer httpOnly cookies.
- `NEXT_PUBLIC_*` / `VITE_PUBLIC_*` env vars ship to the browser — flag secrets in those.

**Configuration**
- Use `process.env` / `import.meta.env` / Next.js conventions — not hardcoded URLs / keys / flags.
- Validate env vars at startup (Zod schema, `t3-env`).
- Centralise feature flags behind one API (`useFeatureFlag('foo')`).
- Route paths as constants (`ROUTES.PROFILE`) — flag magic route strings.

**Module / package structure**
- Feature folders over `components/` + `hooks/` siblings.
- Lazy-load heavy routes with `React.lazy` + `Suspense`.
- Barrel files (`index.ts`) that pull unused exports hurt tree-shaking.

**Tests — React specialisation** (charter covers generic test quality)
- **React Testing Library**: test behaviour through the DOM. `getByRole` over `getByTestId`. `userEvent.setup()` + `await userEvent.x(...)` over `fireEvent`.
- **`findBy*` / `waitFor`** for async — never `setTimeout` in tests.
- **MSW** at the network level for data-fetching tests — beats mocking modules.
- **`it.each` / `test.each`** when 3+ tests differ only in input/output.
- **Factories** (`buildUser(overrides)`) instead of inline object construction.
- **`renderHook`** from `@testing-library/react` for testing custom hooks in isolation.
- Snapshot tests only with meaningful assertions; snapshot-only tests rot.
