# Repair checklist

Process: `repair.v1`
Issue: `#300`
Correlation ID: `33156820757`

- [x] Reproduce the missing `.env.example` failure.
- [x] Remove stale ignore rules so the template remains trackable.
- [x] Document only supported planner settings without credentials.
- [x] Expose the declared OneDev and Validator build, test, and health gates.
- [x] Install the declared runtime extra in CI so the full suite can collect.
- [x] Keep the OneDev candidate build offline and isolated from dependency resolution.
