Score each candidate name for this software product.

Candidates: {words}

Return a JSON array, one object per candidate, in the same order, with exactly these keys:
  "word": the candidate, unchanged
  "fit": 1-5, how strong is the intuitive conceptual bridge to clarity, structure, focus, or utility
  "memorability": 1-5, how easily it is remembered and repeated after one hearing
  "why": at most 12 words on the conceptual bridge (or why it makes no sense)
  "risk": at most 8 words on the main brand risk, or "" if none

Return only the JSON array. No prose, no code fences.
