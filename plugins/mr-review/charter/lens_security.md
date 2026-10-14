# LENS — security and trust boundaries

Read this because the code you are reviewing checks permissions, handles identity or tenancy, or
sits at a boundary where input arrives or an external system is called. It adds questions to your
pass; it does not change your remit.

A security finding needs **the entry point, the path and the sink**, or **the check and the path
that bypasses it**. No hypothetical attackers, no "should use SecureRandom" for a non-secret id.

## Ask

- **Is the authorisation check on every path to the resource?** A new endpoint, a new branch, a
  new bulk variant of an existing operation, an internal method that used to be private. Name the
  path that reaches the resource without passing the check.
- **Is the tenant or owner scope applied at the query, or after?** Filtering in memory after a
  cross-tenant read is a leak the moment the filter is skipped; a query without the tenant clause
  is the leak itself.
- **Does user-controlled input reach a sink unchecked?** Query construction, shell, template,
  deserialiser, file path, redirect URL, log line. Quote the parameter, the hop, the sink.
- **Is identity taken from the request or from the session?** A `userId` in the body or the path
  that the code trusts over the authenticated principal.
- **What does the external call trust?** Response fields used without validation as keys, paths
  or amounts; a status code other than success treated as success; a timeout that is absent.
- **Secrets.** A credential, token or key in code, config committed to the repo, or a log line
  you can point at.
- **Error responses that reveal.** Stack traces, internal ids, "user not found" versus "wrong
  password" on a login path.

## Do not flag

- Anything that requires the attacker to already control the server.
- Randomness quality for non-security identifiers.
- Missing rate limiting, CSRF or headers unless the change is the endpoint or middleware that
  should own them.
- Input validation on data that arrives from an already-validated internal caller.
