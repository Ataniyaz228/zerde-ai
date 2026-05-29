"""Label-free eval harness for ZERDE (Phase 0).

Tests the deterministic grounding/retrieval layer (S1 ingest + S2.5 regex
extract + S6 metadata matching against the cache) WITHOUT calling the LLM.
This is the layer that produces false-confirms, so measuring it is $0 and fast.

Metrics:
  - grounding_rate       : % of clean claims whose (law_id, article) grounds
                           to a real chunk in the cache = coverage potential.
  - false_grounding_rate : % of MUTATED claims (corrupted law_id / article)
                           that still ground = precursor to false-confirm.
  - stability            : deterministic path run twice → identical result.
"""
