# LENS — money, pricing and arithmetic that decides

Read this because the code you are reviewing computes a price, a discount, a tax, a fee, a
currency amount or a rounded figure. It adds questions to your pass; it does not change your
remit. Being wrong here is silent and expensive, and the defects are small: one operator, one
rounding call, one unit.

## Ask

- **What is the type?** `double`/`float` for money is a finding on its own; say where the first
  cent is lost. `BigDecimal` without a `MathContext` or scale is arithmetic waiting to throw.
- **What is the rounding rule, and is it stated?** Half-up, half-even, truncation; to which unit;
  at which step. Rounding before a multiplication and after it give different totals. Quote the
  call and say which reading you had to choose between.
- **Is the unit named?** Pence or pounds; cents or dollars; a percentage as `0.2` or `20`; a rate
  per day or per month. A conversion with no unit in any name is a decision the next reader has to
  reverse-engineer.
- **Where is the currency?** An amount without a currency, or two amounts added without checking
  they share one.
- **Are the boundary conditions right?** Zero quantity, negative adjustment, 100% discount, a
  cap that is inclusive or exclusive, a tier boundary hit exactly.
- **Does the total still equal the sum of the parts** after per-line rounding? Which side is
  authoritative?
- **Is the rule hardcoded?** A rate, a threshold or a fee written as a literal is a finding when
  it will change and a name would say what it is.
- **Is the calculation on the object that owns the data**, or in a service reading fields off it?

## Do not flag

- `* 2`, `/ 100` next to the word percent, an index of `0` — arithmetically obvious numbers.
- Money types the codebase already uses consistently.
- Formatting of amounts for display, unless the formatting *changes* the value.
