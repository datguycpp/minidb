# MiniDB

A SQL database engine built as a conformance project, scored against real SQLite.

- **Goal:** maximize the conformance pass rate over time. Every milestone reports a score
  line: `SCORE <tier>.<n>: <passed>/<total> (<pct>%) [Δ vs last]`.
- **Reference:** SQLite 3.54.0, pinned. SQLite's quirks are ground truth.
- **Rules of the road** (ladder, gates, scoring policy, budget): [STATE.md](STATE.md).
- **Roles:** Research owns corpus/fuzzer/benchmarks, Build owns engine code, Default owns
  the backlog and review. Nobody writes engine code who isn't Build; nobody reports a
  score without raw test output behind it.

Status and next tasks: [STATE.md](STATE.md).
