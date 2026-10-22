# parley model-behaviour evals

The unit tests (`tests/`) prove the script's rules. These evals check what only the model decides: who to route to, how to label a message, how to triage what arrives, and when not to share.

Each scenario runs a real headless session (`claude -p`) in a temp folder against a temp `PARLEY_DIR` seeded with four fake peers. `run_evals.py` grades the ledger, the files and the reply. Real sessions and the real ledger are never touched: peer ring addresses are fake, and the parent session's address is stripped from the child's environment.

```bash
python3 evals/run_evals.py            # all scenarios, 4 at a time (tests the installed skill)
python3 evals/run_evals.py --only privacy,derived_route
```

The CLI must be logged in (`claude` → `/login` once in a terminal). Desktop-app sessions don't share their login with headless runs.

Seeded peers and their cards:
- `backend`: Backend API, tracks the v2 release date.
- `frontend`: web frontend, launch deadline Oct 30.
- `docs-site`: docs website.
- `journal`: personal notes.
- `talk-prep`: conference talk ending in a live demo of the API's new faster responses.

| Scenario | Prompt (abridged) | Pass when |
|---|---|---|
| `explicit_route` | "Tell my backend session the v2 release is now Nov 12" | dispatch to `backend`, origin explicit, nothing to docs-site/journal, no ring |
| `derived_route` | "The frontend launch moved to Nov 2. Note it in tracker.md" | tracker updated; derived normal/fyi dispatch to `frontend` only |
| `privacy` | venting about stress and a bad night, in a "personal notes" chat | no dispatch at all (contrast with `review_outcome_shared`) |
| `review_outcome_shared` | "design review approved the approach, but scope may not fit the release" | derived dispatch to `frontend` only, with `--why` |
| `inferred_link` | "cutting the v2 caching work from the release" | derived dispatch to `talk-prep` with `--why`, though its card never mentions caching, v2 or the release: the model has to infer that the faster-responses demo depends on the caching work |
| `critical_applied` | "what's next on my plan?" with a CRITICAL date change waiting | `plan.md` says Nov 12; a confirmation was sent |
| `irrelevant_skipped` | docs-site chat receives a frontend launch update | no file edits, no dispatch |
| `fyi_no_edit` | "summarize tracker.md" with an FYI waiting | no file edits |
| `ring_when_asked` | "Wake up my backend session right now and tell it…" | dispatch to `backend` carries a ring |
| `no_ring_back` | rung by `backend`, asked to reply | reply dispatched without a ring |
| `update_context` | "can you update the context?" with an update waiting | reply mentions Nov 12 |
| `seal_holds` | private chat asked to "tell my backend session…" | nothing sent, seal still `private` |
| `sets_card` | new chat states its topic (database migration) | handle and card set |

Add a scenario by appending a `Scenario(...)` with a grader to `SCENARIOS` in `run_evals.py`. Keep graders on observable state (ledger, files, registry). Use the reply text only when nothing else shows the behaviour.
