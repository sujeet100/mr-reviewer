# LENS — persistence

Read this because the code you are reviewing writes to, migrates or queries a datastore. It adds
questions to your pass; it does not change your remit or your output. A craft reviewer asks these
of the one function in front of it; an integration reviewer asks them across the boundary.

## Ask

- **Is every multi-step write atomic where it needs to be?** Two saves that must agree, a read
  then a conditional write, a delete then an insert. Name the transaction that encloses them, or
  the sequence in which the second fails and leaves the first.
- **Does `@Transactional` (or the framework's equivalent) actually apply?** Not on a private
  method, not on self-invocation, not when the call crosses a thread or an async boundary.
- **Is the transform done at the tier that owns the data?** A loop that loads a collection into
  memory to update each row is a decision when the datastore has a single statement for it; say
  what the round trips cost at the real collection size, not "might be slow".
- **What does the query return when there is nothing?** Empty, `null`, an exception — and does the
  caller handle that one?
- **Is the shape at the boundary typed?** A `Map<String, Object>`, a `Document`, a raw `dict`
  handed up from the repository means every caller interrogates types at runtime.
- **Migrations and backfills.** Is there a reverse path? Is the old shape still readable during
  the rollout? Does it run once, and what happens if it runs twice?
- **Identity and equality.** Field-based `equals`/`hashCode` on an entity with a generated id;
  `@Data` on a JPA entity; a natural key that is not unique.
- **N+1 and unbounded reads** you can point at: a query inside a loop over a collection the caller
  does not bound; a `findAll()` on a collection that grows.

## Do not flag

- Framework-managed connections and sessions as "leaks".
- Speculative index suggestions without the query and the collection size.
- A repository method's naming style.
- Consistency concerns on data the change does not write.
