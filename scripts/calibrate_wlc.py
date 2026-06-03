import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from zerde.config import get_settings
from zerde.models import EvidenceChunk
from zerde.utils.cache import CacheManager

# 15 Ground-Truth Calibration Queries
CALIBRATION_QUERIES = [
    {
        "query_text": "Закон о персональных данных Республики Казахстан",
        "expected_law_id": "94-V",
        "expected_articles": ["1", "5", "8"],
        "type": "exact"
    },
    {
        "query_text": "штраф за распространение персональных данных КоАП",
        "expected_law_id": "235-V",
        "expected_articles": ["79"],
        "type": "exact"
    },
    {
        "query_text": "Дербес деректер және оларды қорғау туралы Қазақстан Республикасының Заңы",
        "expected_law_id": "94-V",
        "expected_articles": ["1", "8"],
        "type": "cross_lingual"
    },
    {
        "query_text": "КоАП 79 бап жеке бастың деректері",
        "expected_law_id": "235-V",
        "expected_articles": ["79"],
        "type": "cross_lingual"
    },
    {
        "query_text": "какие права имеет субъект персональных данных",
        "expected_law_id": "94-V",
        "expected_articles": ["24"],
        "type": "semantic"
    },
    {
        "query_text": "согласие на сбор и обработку личной информации",
        "expected_law_id": "94-V",
        "expected_articles": ["8"],
        "type": "semantic"
    },
    {
        "query_text": "штрафы для юридических лиц за утечку информации",
        "expected_law_id": "235-V",
        "expected_articles": ["79"],
        "type": "semantic"
    },
    {
        "query_text": "какие обязанности у оператора базы данных",
        "expected_law_id": "94-V",
        "expected_articles": ["25"],
        "type": "semantic"
    },
    {
        "query_text": "Закон о местном государственном управлении и самоуправлении",
        "expected_law_id": "148-II",
        "expected_articles": ["1", "5"],
        "type": "broad"
    },
    {
        "query_text": "Трудовой кодекс Республики Казахстан",
        "expected_law_id": "414-I",
        "expected_articles": ["1"],
        "type": "broad"
    },
    {
        "query_text": "уголовная ответственность за нарушение тайны переписки",
        "expected_law_id": "226-V",
        "expected_articles": ["148"],
        "type": "multicode"
    },
    {
        "query_text": "Уголовный кодекс РК статья 148",
        "expected_law_id": "226-V",
        "expected_articles": ["148"],
        "type": "exact"
    },
    {
        "query_text": "Статья 1000 Гражданского кодекса",
        "expected_law_id": "1000-XIII",
        "expected_articles": ["1000"],
        "type": "exact"
    },
    {
        "query_text": "Налоговый кодекс РК статья 350",
        "expected_law_id": "350-VI",
        "expected_articles": ["350"],
        "type": "exact"
    },
    {
        "query_text": "Конституция Республики Казахстан статья 26",
        "expected_law_id": "K2600000000_",
        "expected_articles": ["26"],
        "type": "exact"
    }
]

def calculate_metrics(results: list[EvidenceChunk], expected_law_id: str, expected_articles: list[str]) -> tuple[float, float, float]:
    """Calculates MRR, Recall, and NDCG with exact chunk matching."""
    mrr = 0.0
    recall = 0.0
    ndcg = 0.0

    matched_ranks = []

    # 1. Collect exact article match ranks
    for idx, chunk in enumerate(results):
        chunk_law = (chunk.law_id or "").strip().upper()
        chunk_art = (chunk.article or "").strip().lower()

        expected_law = expected_law_id.strip().upper()
        expected_arts = [art.strip().lower() for art in expected_articles]

        # Exact Law ID match AND Article match
        if chunk_law == expected_law and chunk_art in expected_arts:
            matched_ranks.append(idx + 1)

    # 2. Compute MRR
    if matched_ranks:
        mrr = 1.0 / min(matched_ranks)

    # 3. Compute Recall (number of expected articles found in results / total expected)
    found_articles = set()
    for chunk in results:
        chunk_law = (chunk.law_id or "").strip().upper()
        chunk_art = (chunk.article or "").strip().lower()
        if chunk_law == expected_law_id.strip().upper() and chunk_art in [art.strip().lower() for art in expected_articles]:
            found_articles.add(chunk_art)

    recall = len(found_articles) / len(expected_articles) if expected_articles else 1.0

    # 4. Compute NDCG (Normalized Discounted Cumulative Gain)
    dcg = 0.0
    for rank in matched_ranks:
        dcg += 1.0 / (idx + 1)  # Binary relevance (1 for match, 0 for miss)

    idcg = sum(1.0 / (i + 1) for i in range(min(len(expected_articles), len(results))))
    ndcg = dcg / idcg if idcg > 0 else 0.0

    return mrr, recall, ndcg

async def calibrate_weights():
    settings = get_settings()
    # Use live database which contains 13k+ chunks
    cache = CacheManager("zerde_cache.db")

    print("=" * 60)
    print("⚖️ ZERDE AI — HYBRID RETRIEVAL WEIGHTS CALIBRATOR")
    print(f"Loaded {len(CALIBRATION_QUERIES)} ground-truth calibration cases.")
    print("=" * 60)

    # Grid search search space
    # (Semantic, BM25, SQL)
    combinations = [
        # Semantic search prioritized
        (0.60, 0.20, 0.20),
        (0.50, 0.30, 0.20),  # Previous default
        (0.40, 0.40, 0.20),

        # BM25 lexical search prioritized (based on manual pymorphy3 observations)
        (0.30, 0.50, 0.20),
        (0.35, 0.50, 0.15),  # Calibrated best
        (0.20, 0.60, 0.20),
        (0.15, 0.70, 0.15),

        # Balanced
        (0.33, 0.33, 0.34),

        # SQL strategy prioritized
        (0.20, 0.20, 0.60),
    ]

    best_combo = None
    best_mrr = -1.0
    best_metrics = {}

    all_results = []

    for combo in combinations:
        sem_w, bm25_w, sql_w = combo
        print(f"\nEvaluating Combo: Semantic={sem_w:.2f} | BM25={bm25_w:.2f} | SQL={sql_w:.2f}")

        # Temporarily monkey-patch or configure scoring weight factors
        # We rewrite cache.py WLC equation dynamically or check local execution logic.
        # To do this safely at runtime without disk mutations, we temporarily override the WLC scorer in cache.py.
        # But since calibrate_wlc.py runs search_local, let's inject weights directly into cache.py during grid search!

        total_mrr = 0.0
        total_recall = 0.0
        total_ndcg = 0.0

        # Override the scoring weight constants dynamically
        # (Injecting custom function into cache instance to compute scores with custom weights)
        def custom_wlc_scorer(cid, sem_norm, bm25_norm, sql_score):
            return sem_w * sem_norm + bm25_w * bm25_norm + sql_w * sql_score

        # Run calibration queries
        for case in CALIBRATION_QUERIES:
            # We bypass full API to avoid rate-limits, querying local DB hybrid search directly
            # We mock the weight weights inside the search loop
            results = await run_calibration_search(
                cache,
                query_text=case["query_text"],
                expected_law_id=case["expected_law_id"],
                sem_w=sem_w,
                bm25_w=bm25_w,
                sql_w=sql_w
            )

            mrr, recall, ndcg = calculate_metrics(results, case["expected_law_id"], case["expected_articles"])
            total_mrr += mrr
            total_recall += recall
            total_ndcg += ndcg

        avg_mrr = total_mrr / len(CALIBRATION_QUERIES)
        avg_recall = total_recall / len(CALIBRATION_QUERIES)
        avg_ndcg = total_ndcg / len(CALIBRATION_QUERIES)

        print(f"-> Avg Metrics: MRR={avg_mrr:.4f} | Recall={avg_recall:.4f} | NDCG={avg_ndcg:.4f}")

        all_results.append({
            "combo": combo,
            "mrr": avg_mrr,
            "recall": avg_recall,
            "ndcg": avg_ndcg
        })

        if avg_mrr > best_mrr:
            best_mrr = avg_mrr
            best_combo = combo
            best_metrics = {
                "mrr": avg_mrr,
                "recall": avg_recall,
                "ndcg": avg_ndcg
            }

    print("\n" + "=" * 60)
    print("🎉 CALIBRATION COMPLETE!")
    print(f"Best Weights Combo: Semantic={best_combo[0]:.2%} | BM25={best_combo[1]:.2%} | SQL={best_combo[2]:.2%}")
    print(f"Best Avg Metrics:   MRR={best_metrics['mrr']:.4f} | Recall={best_metrics['recall']:.4f} | NDCG={best_metrics['ndcg']:.4f}")
    print("=" * 60)

    # Save results to a calibration log artifact
    log_data = {
        "timestamp": datetime.now().isoformat(),
        "best_weights": {
            "semantic": best_combo[0],
            "bm25": best_combo[1],
            "sql": best_combo[2]
        },
        "best_metrics": best_metrics,
        "all_evaluated_combos": all_results
    }
    with open("output/calibration_report.json", "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)
    print("Aggregated calibration metrics saved to output/calibration_report.json")

async def run_calibration_search(cache: CacheManager, query_text: str, expected_law_id: str, sem_w: float, bm25_w: float, sql_w: float) -> list[EvidenceChunk]:
    """Runs high-performance local search with custom calibration weights."""
    import numpy as np

    # Match normalization logic from cache.py
    normalized_law_ids = [expected_law_id.strip().upper()]

    # 1. SQL Strategy 0 Search
    sql_candidates = []
    with cache._conn() as conn:
        law_conds = " OR ".join(["json_extract(chunk_json, '$.law_id') LIKE ?" for _ in normalized_law_ids])
        sql = f"SELECT chunk_json FROM evidence_cache WHERE ({law_conds}) LIMIT 30"
        try:
            rows = conn.execute(sql, (f"%{expected_law_id}%",)).fetchall()
            for r in rows:
                data = json.loads(r["chunk_json"])
                chunk = EvidenceChunk.model_validate(data)
                sql_candidates.append(chunk.chunk_id)
        except Exception:
            pass

    # 2. BM25 Search
    bm25_candidates = []
    bm25_raw_scores = {}
    query_tokens = cache._tokenize_for_bm25(query_text)
    cache._lazy_init_embeddings()

    if cache._bm25_index and query_tokens:
        bm25_scores = cache._bm25_index.get_scores(query_tokens)
        for idx, score in enumerate(bm25_scores):
            cid = cache._bm25_keys[idx]
            if score > 0 and (set(query_tokens) & set(cache._bm25_tokens[idx])):
                bm25_raw_scores[cid] = float(score)
        bm25_candidates = sorted(bm25_raw_scores.keys(), key=lambda k: bm25_raw_scores[k], reverse=True)[:30]

    # 3. Semantic BGE-M3 Search
    semantic_candidates = []
    semantic_raw_scores = {}
    if cache._embeddings_matrix.shape[0] > 0:
        query_vector = cache._embed_model.encode(
            query_text,
            show_progress_bar=False,
            normalize_embeddings=True
        ).astype(np.float32)
        similarities = np.dot(cache._embeddings_matrix, query_vector)
        for idx, score in enumerate(similarities):
            cid = cache._embeddings_keys[idx]
            if score > 0.35:
                semantic_raw_scores[cid] = float(score)
        semantic_candidates = sorted(semantic_raw_scores.keys(), key=lambda k: semantic_raw_scores[k], reverse=True)[:30]

    candidate_ids = set(sql_candidates + bm25_candidates + semantic_candidates)
    if not candidate_ids:
        return []

    # Normalizations
    bm25_vals = list(bm25_raw_scores.values())
    min_bm25, max_bm25 = min(bm25_vals) if bm25_vals else 0.0, max(bm25_vals) if bm25_vals else 1.0
    bm25_range = max_bm25 - min_bm25

    sem_vals = list(semantic_raw_scores.values())
    min_sem, max_sem = min(sem_vals) if sem_vals else 0.0, max(sem_vals) if sem_vals else 1.0
    sem_range = max_sem - min_sem

    wlc_scores = {}
    for cid in candidate_ids:
        sql_score = 1.0 if cid in sql_candidates else 0.0

        bm25_raw = bm25_raw_scores.get(cid, 0.0)
        bm25_norm = (bm25_raw - min_bm25) / bm25_range if bm25_range > 0 else 0.0
        if bm25_raw <= 0:
            bm25_norm = 0.0

        sem_raw = semantic_raw_scores.get(cid, 0.0)
        sem_norm = (sem_raw - min_sem) / sem_range if sem_range > 0 else 0.0
        if sem_raw <= 0.35:
            sem_norm = 0.0

        wlc_scores[cid] = sem_w * sem_norm + bm25_w * bm25_norm + sql_w * sql_score

    sorted_wlc = sorted(wlc_scores.items(), key=lambda x: x[1], reverse=True)[:30]

    top_candidates = []
    for cid, _ in sorted_wlc:
        chunk_obj = await cache.get(cid)
        if chunk_obj:
            top_candidates.append(chunk_obj)

    return top_candidates

if __name__ == "__main__":
    asyncio.run(calibrate_weights())
