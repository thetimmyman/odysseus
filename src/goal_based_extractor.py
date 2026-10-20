# src/goal_based_extractor.py
"""
Goal-based content extraction prompt inspired by Alibaba Tongyi DeepResearch.
"""

EXTRACTOR_SYSTEM = """Extract relevant information from a webpage for a given research goal.

Goal: {goal}

Task guidelines:
1. Locate the specific sections directly related to the goal within the provided webpage content.
2. Identify and extract the most relevant information; output full original context where possible, up to three or more paragraphs.
3. Organize into a concise paragraph with logical flow, judging each piece of information's contribution to the goal.

Respond in JSON with exactly these fields: "rational", "evidence", "summary", "quotes".

- "rational": why this section matters for the goal.
- "evidence": the supporting context from the page.
- "summary": a concise summary of how this information answers the goal.
- "quotes": an array of at most 5 strings. Each string must be an EXACT,
  contiguous span copied character-for-character from the webpage content,
  at most 400 characters long. Do not paraphrase, summarise, fix typos, join
  separate passages with "..." or add words that are not on the page. Use an
  empty array when no exact span supports the goal.

Example:
{{
    "rational": "This section discusses X which directly relates to the goal of understanding Y",
    "evidence": "Full quotes and context from the page...",
    "summary": "Concise summary of how this information answers the goal",
    "quotes": ["An exact sentence copied from the page.", "Another exact span."]
}}
"""
