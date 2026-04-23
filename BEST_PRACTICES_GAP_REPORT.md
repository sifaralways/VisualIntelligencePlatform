# VIP Frontend/Backend Best-Practices Gap Report

Date: 2026-04-15
Scope: backend and frontend engineering practices, delivery safety, and maintainability.

## Prioritized Opportunities

| # | Opportunity | Expected Improvement | Criticality | Complexity | Risk | Estimate | ROI | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1 | Establish CI quality gates (lint + typecheck + tests) | Prevent regressions from reaching main; catch syntax/runtime issues earlier | High | Medium | Low | 1-2 days | High | No `.github` workflows found; frontend scripts only include `dev/build/lint/preview` in `frontend/package.json`; no backend test execution path in repo root scripts |
| 2 | Add automated test suites (backend API + frontend component/integration) | Higher release confidence for multi-turn chat, clustering, and popup flows | High | Medium | Medium | 3-6 days initial baseline | High | No test files found by `**/*test*.*`; no `pytest.ini`/`pyproject.toml` test config files found |
| 3 | Unify Python dependency management and lock strategy | Reproducible environments and fewer install/version drift incidents | High | Medium | Medium | 1-2 days | High | `requirements.txt` has mostly unpinned packages and duplicated `httpx` entries; `setup.sh` installs a separate hardcoded package list |
| 4 | Remove dependency contradiction (`hdbscan`) between docs and setup | Eliminate ambiguous runtime behavior and onboarding confusion | High | Low | Low | 1-2 hours | High | `requirements.txt` says standalone `hdbscan` removed, while `setup.sh` still installs `hdbscan` |
| 5 | Tighten backend production posture (env-specific CORS/methods/headers/docs) | Reduced accidental exposure when moving beyond local dev | Medium | Low | Low | 0.5-1 day | Medium-High | `backend/main.py` allows wildcard methods and headers and always serves `/docs` and `/redoc` |
| 6 | Add structured request/trace logging and correlation IDs | Faster root-cause analysis for inconsistent intent/query outcomes | Medium | Medium | Low | 1-2 days | Medium-High | Logging exists in `backend/main.py` but no explicit request-scoped correlation/trace IDs are visible |
| 7 | Break down oversized hotspot modules | Lower change risk, easier reviews, and fewer merge conflicts | High | High | Medium | 5-10 days incremental | High | File sizes: `frontend/src/pages/PeoplePage.tsx` 1963 lines, `backend/assistant/executor.py` 1705 lines, `backend/api/routes/persons.py` 1525 lines |
| 8 | Introduce backend static analysis/type checks (`ruff` + `mypy`/`pyright`) | Earlier detection of logic/type defects in complex async/ML orchestration | Medium | Medium | Low | 1-2 days | Medium | Current repo shows linting only for frontend (`frontend/package.json` and `frontend/eslint.config.js`) |
| 9 | Upgrade frontend linting to type-aware and stricter rules | Catch subtle TS/React issues pre-runtime, reduce UI regressions | Medium | Low-Medium | Low | 0.5-1 day | Medium | `frontend/eslint.config.js` uses `typescript-eslint` recommended only (not type-aware project configuration) |
| 10 | Create API contract/versioning discipline for frontend-backend | Fewer breaking changes in chat/people endpoints and safer iterations | Medium | Medium | Medium | 2-4 days | Medium-High | Rapidly evolving API surfaces (`/api/chat`, `/api/persons`) with tight coupling to `frontend/src/api/client.ts` patterns |
| 11 | Align and continuously verify project documentation | Reduce onboarding errors and support burden | Medium | Low | Low | 0.5 day | Medium | `README.md` tech stack says React 18/Vite 5 while `frontend/package.json` is React 19/Vite 7 |
| 12 | Replace manual startup scripts with managed dev tasks/process supervision | More reliable startup/shutdown and fewer orphaned processes | Low-Medium | Low-Medium | Low | 0.5-1 day | Medium | `start.sh` runs two background processes with manual signal cleanup and no health-based orchestration |

## Quick Wins (Do First)

1. Resolve dependency drift: make `setup.sh` install from `requirements.txt` (or generated lock) and remove duplicated/contradictory entries.
2. Add CI workflow for frontend lint/typecheck/build and backend lint/test smoke.
3. Split `PeoplePage.tsx` into feature components + hooks to reduce incident-prone surface area.
4. Update README stack/version claims to match current dependencies.

## Suggested 30-Day Execution Plan

1. Week 1: CI baseline, dependency unification, README correction.
2. Week 2: Backend and frontend test skeletons (high-risk flows first: chat clarification, ignore suggestion UI).
3. Week 3: Frontend lint strictness + backend static analysis integration.
4. Week 4: Module decomposition (People page + assistant executor) and request correlation logging.

## Scoring Definitions

- Criticality: impact on correctness, security, delivery reliability.
- Complexity: implementation effort and refactor breadth.
- Risk: probability of temporary disruption while implementing.
- Estimate: engineering time for first production-usable version.
- ROI: expected return over next 1-3 release cycles.
