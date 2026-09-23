# Agent instructions — Common repository

## Read first

- Follow [README.md](README.md), especially service boundaries, communication contracts, contribution workflow, coding standards, testing and repository hygiene. These are requirements, not suggestions; mentors may check them automatically.
- Read the relevant issue and [PR template](.github/PULL_REQUEST_TEMPLATE.md) before editing. Re-read these files after updating from `main`.
- Keep these instructions aligned with the README. Do not silently change requirements, lower coverage, skip checks or weaken branch protection to make work pass. Report contradictions instead of inventing a rule.
- This file governs common-repository work. Each initialized service submodule is a separate Git repository: read its own `AGENTS.md` and README, use its branch/remote and apply its service-specific rules. The common repository’s two-approval rule does not apply to the single-maintainer services.

## Before each new task

1. Confirm the repository, remote, branch and worktree state:

   ```sh
   git rev-parse --show-toplevel
   git remote -v
   git branch --show-current
   git status --short --branch
   ```

   The intended GitHub repository is `AlexDandy77/tamagotchi-go`. Inspect dirty files and submodule pointers; never discard, reset, stash or commit somebody else's work automatically. If unrelated work prevents a clean start, use an isolated worktree or ask how to preserve it.
2. Fetch all configured remotes and tags, then fast-forward local `main` before starting a new task:

   ```sh
   git fetch --all --prune --tags
   git switch main
   git pull --ff-only origin main
   ```

   Run this only after preserving existing work. If fetching fails or `main` has diverged, resolve or report it before editing; do not silently work from stale history or reset `main`.
3. Read issues using GitHub CLI:

   ```sh
   gh issue list --repo AlexDandy77/tamagotchi-go --state open
   gh issue view <number> --repo AlexDandy77/tamagotchi-go --comments
   ```

   Read an issue only when its number is known. For shared requirements, also check `AlexDandy77/tamagotchi-go`. Do not invent an issue or close one for partial work. If none applies, proceed with the user's request and say so in the PR.
4. Create a task branch from updated `main` **before editing**. Use `<type>/common/<short-description>` in lowercase with hyphens. Types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`. Use the type matching the work; documentation uses `docs`, not `feat`.

   ```sh
   git switch -c docs/common/agent-instructions
   ```

   This is an example name; choose an unused name for each new task. When continuing an existing task or PR, keep its branch, fetch and compare with `origin/main`; do not create another branch or abandon uncommitted work. Rebase a clean task branch when needed. After a rebase, only use `--force-with-lease` on your own task branch, never on `main` or another contributor's branch.

## Implementation rules

- Preserve service ownership and the architecture in the README. Do not introduce shared database writes, client-controlled outcomes or undocumented service dependencies.
- Keep `contracts/openapi.yaml`, `contracts/events.schema.json`, `contracts/realtime.schema.json`, `contracts/field-dictionary.md`, `contracts/game-rules.md`, README catalogs and examples consistent. Update affected service documentation when an interface changes.
- Validate inputs and permissions, preserve idempotency and version checks, and follow the documented HTTP deadlines, error responses, Kafka outbox/inbox and retry rules.
- For code changes, follow README layers, repository interfaces, explicit transactions, injected clocks, validated environment configuration and structured request-ID logs. Avoid unbounded queries, hardcoded rules, swallowed errors and duplicated middleware.
- Breaking HTTP changes require a new API version; incompatible events require a new suffix. Keep the contract's `info.version` separate from repository release tags.

## Validation before committing

- Run the repository's contract checks using the README commands:

  ```sh
  python3 -m venv .venv
  .venv/bin/python -m pip install -r .github/scripts/requirements.txt
  .venv/bin/python .github/scripts/check_contracts.py
  ```

- The **Validate contracts** CI check must pass. It runs without access to private submodules; do not make common validation depend on private service checkouts.
- For changed service code, follow that service's tests and the README minimums: **80% statement coverage and 70% branch coverage per service**, enforced from the first implementation PR. Go uses `testing`, `net/http/httptest` and `gobco`; TypeScript uses Vitest. Run applicable contract and worker tests too.
- Run `git diff --check`, inspect the complete diff and inspect `git diff --cached` before each commit. Stage intended files explicitly; do not use `git add .` when unrelated changes or submodule pointers are present.
- Report exact checks and outcomes, including missing dependencies or unavailable checks. Never claim an unrun or failed check passed; do not bypass mentor or CI verification.

## Hygiene

- Follow `.gitignore`; never commit secrets, real `.env` files, keys/tokens, installed dependencies, binaries, coverage reports, logs, local databases or editor/OS files. Do not print secrets in logs or PRs.
- Commit source, tests, docs, manifests/lockfiles, Docker/CI configuration and `.env.example` containing only documented placeholders.
- If a secret is exposed, report it and arrange immediate rotation. Deleting it in a later commit is insufficient; coordinate history cleanup with the owner, especially on protected `main`.

## Commits, push and PRs

- Commit titles are a single line: `type(common): imperative summary`. Use the allowed types above, focused commits, no description body and no co-author trailers. Reference a real completing issue in the title when applicable; use `refs` for partial work and `closes` only for completed scope. Qualify issues in another repository (for example `AlexDandy77/tamagotchi-go#7`).
- When delivery is requested, commit and push the task branch, then create or update a PR with `gh`. Read the actual `.github/PULL_REQUEST_TEMPLATE.md` each time and preserve its headings. Explain the change and why, include validation results in the description, and fill the related issue section accurately.

  ```sh
  git push -u origin <task-branch>
  gh pr list --repo AlexDandy77/tamagotchi-go --head <task-branch> --state open
  gh pr create --repo AlexDandy77/tamagotchi-go --base main --head <task-branch> --title '<conventional title>' --body-file <prepared-markdown-file>
  gh pr checks <pr-number> --repo AlexDandy77/tamagotchi-go
  ```

- Reuse an existing PR instead of making duplicates (`gh pr edit --body-file`). Do not use `--fill` instead of the template. Pass multiline descriptions via a file, not shell-interpolated strings. Inspect checks after creation; report pending, absent or failed checks truthfully.
- Common `main` requires **two approvals**, resolved review conversations, a current branch and passing **Validate contracts**. New pushes require fresh approvals. Never bypass protection, push directly to `main`, or enable auto-merge without authorization.
- Leave PRs open for the user to review and merge unless they explicitly request merging. Rebase and merge focused PRs; squash many fix-up commits. Delete a task branch only after its work is merged.

## Submodules and releases

- Treat each service as an independent repository. Commit and push inside the service first. After its PR merges, fetch its actual merged commit (rebase/squash can change the hash), then update the common repository's submodule pointer and/or any other lab related files in a separate task branch/PR. Never point the common repository at a local-only commit or silently stage existing pointer changes. Do not initialize or edit unrelated private submodules.
- A documentation commit or an open PR is not automatically a release. When a release is requested and ready, fetch tags, update `main` with `--ff-only`, identify the merged release commit and confirm its checks passed. Common releases must also pin merged, available service commits.
- Choose the next unused `vMAJOR.MINOR.PATCH` from existing tags and the README versioning rules: breaking public contract change → major, compatible feature → minor, compatible fix → patch. Tag only a validated commit on `main`, never an unmerged task branch. If the release scope/version is unclear, clarify before publishing it.

  ```sh
  git tag -a vMAJOR.MINOR.PATCH <validated-main-commit> -m "Release vMAJOR.MINOR.PATCH"
  git push origin refs/tags/vMAJOR.MINOR.PATCH
  ```

- Replace placeholders with the actual version and verified commit. Push only that tag, not all tags. Published tags never move; do not force-update or reuse them. Create a GitHub Release with `gh release create --verify-tag` only when publishing a GitHub Release is part of the request.
- Finish with the branch, commit and PR URL, checks/results, and any remaining merge or release steps. Never report a merge, release or successful CI run that has not happened.
