# Retrieval eval results — Epic 02 (#30)

Aggregate metrics from each retrieval-eval run. The query set itself
(`agent/tests/retrieval_eval_set.jsonl`) is RAW_PERSONAL and gitignored.
Only aggregate numbers — Recall@K, MRR, per-category averages — land here,
so the gate is checkable from the repo without leaking queries.

## Files

- `baseline.json` — locked aggregate from the pre-E02 baseline run, used as
  the "before" reference for the ≥10pp Recall@5 gate. Created by
  `scripts/run_retrieval_eval.py --mode baseline` against the live DB and
  the real eval set.
- `retrieval_baseline_<YYYY-MM-DD>.md` — narrative snapshot of the baseline
  run.
- `retrieval_after_<subissue>_<YYYY-MM-DD>.md` — one per landed sub-issue
  (#27, #28, #29). Captures the delta from baseline.

## Local workflow (each pre-E02 run, then each post-landing run)

```bash
# Populate the gitignored real eval set first (≥30 entries, 5 categories).
$ cp agent/tests/retrieval_eval_set.example.jsonl \
     agent/tests/retrieval_eval_set.jsonl
$ $EDITOR agent/tests/retrieval_eval_set.jsonl

# Baseline — semantic-only path (#27/#28/#29 not yet engaged).
$ .venv/bin/python scripts/run_retrieval_eval.py --mode baseline

# After each sub-issue lands, switch retriever-mode to measure deltas:
$ .venv/bin/python scripts/run_retrieval_eval.py \
    --mode after --tag bm25 --retriever-mode bm25
$ .venv/bin/python scripts/run_retrieval_eval.py \
    --mode after --tag recency --retriever-mode semantic
$ .venv/bin/python scripts/run_retrieval_eval.py \
    --mode after --tag rrf --retriever-mode hybrid

# Final gate check — uses --retriever-mode hybrid against the locked baseline.
$ .venv/bin/python scripts/run_retrieval_eval.py --mode gate --retriever-mode hybrid
```

## Privacy

- Query strings, memory IDs, and per-query verdicts NEVER leave the
  machine. They stay in the gitignored jsonl and in any local-only run
  logs.
- Aggregate metrics (Recall@K averaged, per-category Recall@K averaged,
  MRR averaged) are non-personal by construction and committed here.
- Do not paste per-query output into chat messages or copy it into PR
  descriptions.
