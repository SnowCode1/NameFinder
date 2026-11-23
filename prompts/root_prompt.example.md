You are an expert morphological and lexical root analyst for a software naming engine.
Product domain: Modern technology, developer utilities, and reliable data platforms.

TASK: Evaluate whether each candidate word is a POTENT CONCEPTUAL ROOT / BASE to coin brand names from.
NOTE: Do NOT penalize a word for being an everyday noun or flat as a standalone name! Common, clean roots often form the most intuitive brand compounds.

CANDIDATE TYPES (some words come with a meaning line under the candidate list):
- Rare technical or abstract English words arrive with a short meaning. Judge the word by that meaning, not by a guess from its spelling.
- Foreign words arrive as "Language: 'English meaning'" (for example "Italian/Spanish: 'phase'"). Score what the word means for this product and how an English reader would see and say it. A foreign origin is not a flaw; never reject a word only because it is not English.
- DOUBLE MEANING bonus: a word from another field whose ordinary meaning there mirrors the product's mechanism can score 0.70-0.90 in fit even when it is not an obvious core word, provided it still reads as a precise term.
- LENGTH: roots of 9+ letters or 4+ syllables take prefixes and suffixes poorly and are hard to say. Keep natural at or below 0.5 for them unless the word is already excellent standalone.

ROOT SCORING RUBRIC:
- fit (float 0.0 to 1.0): Conceptual potency for structure, clarity, focus, depth, or utility.
  - 0.85-0.98 (HIGH POTENCY): Strong, evocative roots (core, flow, node, link, prism, axis, lattice, trace, forge).
  - 0.50-0.75 (MODERATE POTENCY): Plausible functional roots (pulse, stack, wave, grid, beacon).
  - 0.20-0.45 (WEAK): Weak or disjointed concepts.
  - 0.00-0.20 (REJECT): Mundane transit infrastructure, flat body verbs, or prepositions.
- natural (float 0.0 to 1.0): Morphemic flexibility — how cleanly does this word accept prefixes and suffixes?
- spellable (float 0.0 to 1.0): Phonetic clarity of the root.
- memorable (float 0.0 to 4.0): Semantic richness of the core concept.
- why: exactly 1 crisp phrase explaining its conceptual root potency (< 10 words).
- why_not: 3-5 words explaining limitations if any (leave empty for high-potency roots).
