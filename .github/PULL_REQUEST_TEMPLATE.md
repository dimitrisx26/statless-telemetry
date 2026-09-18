<!-- Thanks for the PR. Keep it focused: one logical change per PR. -->

## What & why

<!-- What does this change, and what problem does it solve? -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] New feature (SDK API, collector endpoint, metric, setting)
- [ ] Docs / comments only
- [ ] Refactor / internal (no behavior change)

## How tested

<!-- Commands run, plus any manual checks against /v1/telemetry/ping, /v1/stats, or the SDK. -->

- [ ] `cd collector && uv run pytest -q && uv run ruff check app tests`
- [ ] `cd packages/sdk && npm run lint && npm run build && npm run size && npm test`
- [ ] Manually verified the affected endpoint or integration

## Checklist

- [ ] Tests added or updated for behavior changes
- [ ] SDK bundle still under the 2KB gzipped budget (`npm run size`)
- [ ] No secrets, tokens, or raw IPs added
- [ ] No new runtime dependencies in `packages/sdk`
- [ ] README updated if config, endpoints, payload fields, or privacy behavior changed
- [ ] New env vars or settings added to the README configuration table
