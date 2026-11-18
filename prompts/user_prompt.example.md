Evaluate each candidate word for this software product brand.

Candidates: {words}

For each candidate, provide exact numerical scores using the narrative bridge, collision, and early-out rubric:
- "word": the candidate word exactly as given
- "fit": float between 0.0 and 1.0 (<= 0.2 for non-starters; >= 0.8 only for strong conceptual alignment)
- "natural": float between 0.0 and 1.0
- "spellable": float between 0.0 and 1.0
- "memorable": float between 0.0 and 4.0
- "neg": float between 0.0 and 1.0
- "taken": float between 0.0 and 1.0
- "common": float between 0.0 and 1.0
- "why": short string (leave empty '' for rejected non-starters; <= 10 words for viable brands)
- "why_not": short string (3-5 words for non-starters; <= 10 words for viable brands)

Return a JSON array of objects, one per candidate, in the same order.
Return ONLY the raw JSON array. No markdown fences, no prose.
