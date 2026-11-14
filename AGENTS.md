## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, using the default strings. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root plus `docs/adr/`. See `docs/agents/domain.md`.

## Workflow

- One issue per feature or milestone, published through the configured tracker.
- Branch from `main`: `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, or `chore/<slug>`.
- Commit in small, scoped commits with conventional messages.
- Push the branch and open a PR that links the issue (`Closes #<n>`).
- Run `npm run lint` and `npm run build` before opening the PR; add tests for new behavior.
- Merge through the PR; never commit directly to `main`.
- For a multi-ticket spec, `/implement-spec` may use an integration branch: ticket branches merge into it, and the integration branch opens the final PR.
- A milestone is not done until its branch is pushed and its PR is open.

<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
