# ZERDE eval — Phase 0 baseline (label-free)

Recorded 2026-05-29. Command:

```
python scripts/eval/run_eval.py --sample 30 --max-scan 150
```

No LLM is involved — this measures the deterministic grounding/retrieval layer
(S1 ingest + S2.5 regex extract + S6 metadata matching against the SQLite cache).
Run artifacts land in `data/eval/results_<ts>.json` (gitignored); this file is
the committed reference Phase 1 compares against.

## Aggregate (30 stratified bills, 2191 claims with law+article)

| metric | value | target | meaning |
|---|---|---|---|
| `grounding_rate` (clean) | **0.70** | grow | % of claims whose (law_id, article) grounds to a real cache chunk — coverage potential |
| `law_false_grounding_rate` | **0.704** 🔴 | → 0 | corrupted law id (digit transposition) STILL grounds — false-confirm precursor |
| `article_false_grounding_rate` | **0.00** ✅ | → 0 | impossible article (99999) never grounds |
| `stability_all_pass` | **True** ✅ | True | deterministic path identical across two runs |

## Headline finding

`law_false_grounding_rate ≈ 0.70` is driven by `LawRegistry.resolve()`'s `difflib`
fuzzy match at cutoff 0.75 (`law_registry.py:212`): a transposed law id snaps onto
a real law — confirmed directly: `resolve('253-V') -> '235-V'`. So a wrong law
number grounds to the real law's chunks, which in production (with the LLM auditor)
becomes a confident confirmation against the WRONG statute. This is the system's
worst failure mode (false-confirm) and is **Phase 1's primary target**: tighten or
remove the short-id fuzzy match and re-measure — expect this number to collapse.

## Secondary findings (per-bill zeros)

- `414-I` grounding 0.00 — cache stores the law as `414-I-NEW`; `resolve('414-I')`
  does not map to it. law_id-mismatch coverage gap → single-source-of-truth (Phase 1.1).
- `94-V`, `434-V` grounding 0.00 — law absent / under-ingested in cache (coverage, Phase 4).
- `261-IV` grounding 0.24–0.43 — partial cache coverage for ЧСИ articles.

## Caveats

- Large "Сравнительная таблица" documents contribute hundreds of claims each and
  dominate the claim-weighted aggregate. Acceptable for a baseline; consider a
  per-bill claim cap or document-type stratification when this becomes a CI gate.
- Mutation is claim-level (corrupt `target_law_ids` / article number), not raw-text,
  to target the grounding layer precisely. Verdict-level eval (LLM) is Phase 1.
