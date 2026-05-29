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

## Phase 1.1a result (fuzzy-resolve fix)

After replacing `LawRegistry.resolve()`'s `difflib` short-id fuzzy match with
strict base-ID matching + an explicit renumbering-alias table (commit in Phase 1):

| metric | baseline | after 1.1a |
|---|---|---|
| `grounding_rate` (clean) | 0.70 | **0.729** ↑ (base-ID bridging fixed `414-I`→`414-I-NEW`, `226-V`→`226-V-UK`) |
| `law_false_grounding_rate` | 0.704 | **0.000** ✅ |
| `article_false_grounding_rate` | 0.00 | 0.00 |
| `stability_all_pass` | True | True |

(The mutation engine was also corrected to corrupt *all* law refs in a multi-law
claim; corrupting only the first inflated the rate with a harness artifact, not a
resolve bug.)

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

## Gather-path safety net (law_id ↔ adilet consistency)

`python scripts/eval/lawid_consistency.py` — guards the dict-unification refactor
(Phase 1.1) by comparing law_id→adilet_code across all sources. No LLM/network.

Baseline (2026-05-29):

| metric | value | note |
|---|---|---|
| `drift_count` | **0** | sources agree on adilet codes where they overlap (the `87-IV` desync was the one known case, fixed in 1.1b) |
| `registry get_adilet_url match` | **35/35** | registry reproduces every `law_metadata` law's code |
| law_ids in dicts but NOT in `law_metadata` | **21** | would lose coverage if dicts deleted |
| of those, **migration blockers** | **16** | registry can't reproduce → must be ingested into `law_metadata` first |

The refactor risk is therefore **coverage, not drift**. Deleting `_LAW_ID_KNOWN`
(54 entries) today would drop 16 laws onto the registry's `get_adilet_url`
year/zero-pad fabrication, yielding WRONG codes (e.g. `138-IV` → `Z0000000138`).

Pre-deletion checklist (Phase 1.1 unification):
1. Remove `get_adilet_url`'s code fabrication (return None for unknown, like 1.1b did for s3).
2. Migrate the 16 blocker law codes into `law_metadata` (verify against Adilet — note
   `_LAW_ID_KNOWN` has a suspect `309-II → K990000409`, which is 409-I's code).
3. Re-run this checker: `only_in_dicts`/`blockers` → 0, then delete the dicts.

## Caveats

- Large "Сравнительная таблица" documents contribute hundreds of claims each and
  dominate the claim-weighted aggregate. Acceptable for a baseline; consider a
  per-bill claim cap or document-type stratification when this becomes a CI gate.
- Mutation is claim-level (corrupt `target_law_ids` / article number), not raw-text,
  to target the grounding layer precisely. Verdict-level eval (LLM) is Phase 1.
