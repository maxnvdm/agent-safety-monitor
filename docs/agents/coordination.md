# Multi-agent coordination

GitHub issues and pull requests are the coordination system of record. Chat transcripts and personal notes are scratch space, not required context.

## Work lifecycle

1. Begin with an issue containing objective, scope, acceptance criteria, and validation. Add it to the portfolio Project and set Stage.
2. Claim it by assignment where possible and a comment naming the branch/worktree and intended files. Set Stage to `In progress`.
3. Use one branch and one worktree per issue. Do not share a dirty worktree.
4. Keep scope narrow; create another issue for adjacent work. Check active issues and PRs before overlapping files.
5. Link the PR, record validation, and set Stage to `Review`. Do not self-merge unless explicitly authorized.
6. When pausing or finishing, comment with state, decisions, changed files, validation, risks, and exact next action; set Stage to `Blocked` or `Done`.

## Durable knowledge

Stable facts belong in docs linked from `docs/README.md`; consequential decisions in `docs/decisions/`; multi-session plans in `docs/plans/`; agent constraints in `AGENTS.md`. Never commit secrets, raw user session logs, personal data, ephemeral outputs, or unverified guesses. Update stale docs with the code they describe.

## Handoff format

```markdown
### Handoff
- State: in progress | blocked | ready for review
- Branch/worktree: ...
- Completed: ...
- Decisions: ...
- Validation: command -> result
- Risks/unknowns: ...
- Next action: ...
```

Prefer GitHub MCP when available; `gh issue`, `gh pr`, `gh project`, `gh api`, REST, and GraphQL are supported fallbacks. Report any inability to update GitHub in the handoff.
