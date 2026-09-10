## Description

<!-- Clearly explain the problem, what changed, and why. For behavior or contract
changes, describe the resulting behavior with an example when useful. -->

## Validation

<!-- State which checks/tests you ran and their results. For service code,
include coverage and the important success/failure/edge cases checked. -->

## Related issue

<!-- Reference the issue this PR addresses, for example: Closes #4. -->

## Compatibility and follow-up

<!-- Explain breaking contract changes, migration needs, or remaining work.
Write "None" if there are none. For release PRs (dev to main), state the
intended version tag and the notable changes. -->

## Checklist

- [ ] Branch is named `<type>/<scope>/<short-description>` and targets `dev` (or `main` for a release)
- [ ] Commit titles follow Conventional Commits; Squash and merge requested if the PR has about ten or more commits
- [ ] Tests added or updated for new or changed behavior, including failure and edge cases; coverage meets the service threshold
- [ ] No secrets, `.env` files, installed dependencies or build outputs are included
- [ ] Contract, README and examples were updated together if an interface changed
