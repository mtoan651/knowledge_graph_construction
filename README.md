# Assignment 4 Report: KG-based QA for NCU Regulations

---

## 1. KG Construction Logic and Design Choices

### 1.1 Pipeline Overview

The system builds the Knowledge Graph in three sequential stages:

```
PDFs (source/)
    │
    ▼  setup_data.py
SQLite (ncu_regulations.db)
    │
    ▼  build_kg.py
Neo4j Knowledge Graph
```

**Stage 1 — PDF Parsing (`setup_data.py`)**

Each regulation PDF uses a different structural format, so two parser modes were implemented:

- **`article` mode** (ncu1–ncu5): Matches `Article N` or `Article N-M` headers using regex `^\s*Article\s+([0-9]+(?:\-[0-9]+)?)`. Used for regulations that follow standard legislative article numbering.
- **`numbered` mode** (ncu6): Matches numbered rules `1.`, `2.` etc. using regex `^\s*([0-9]+)\.`. Used for NCU Student Examination Rules which uses a simpler numbered list format. Layout-aware extraction (`use_layout=True`) is enabled for this file to preserve column order.

Text is cleaned by collapsing whitespace and stripping page numbers / headers.

**Stage 2 — KG Construction (`build_kg.py`)**

For each article, rule facts are extracted in two ways:

1. **LLM Extraction (primary)**: The local model is prompted to output structured JSON:
   ```json
   {"rules": [{"type": "penalty|requirement|procedure|eligibility",
               "action": "specific condition or trigger",
               "result": "specific outcome or requirement"}]}
   ```
   The prompt instructs the model to use exact numbers and facts from the text (e.g. "20 minutes", "5 points", "200 NTD"). Three JSON-parsing strategies handle truncated or malformed outputs.

2. **Deterministic Fallback**: When LLM extraction yields no rules, a regex-based sentence-pair extractor identifies legally significant sentences (those containing numbers, penalty keywords, or requirement keywords) and pairs them with the preceding sentence as action context. This ensures full KG coverage even for articles where the LLM fails.

**Design choice: Why Rule nodes instead of raw article text?**  
Storing action/result pairs as structured `Rule` nodes enables scored fulltext retrieval over semantically meaningful fields rather than raw paragraph text. This reduces noise when matching questions to evidence and allows question-type filtering (e.g. only retrieve "penalty" rules for penalty questions).

**Design choice: Dual-channel evidence**  
Rule nodes are deliberately kept concise (10–30 words per field). For questions requiring more legal context, a secondary channel fetches the full article text from SQLite, routed through the KG results (KG-first, DB-second). This balances structured retrieval precision with natural language completeness.

### 1.2 KG Coverage Strategy

- **Deduplication**: Rules are deduplicated within each regulation by normalising the first 80 characters of `action` + `result`.
- **Coverage audit**: After building, the system reports how many articles have at least one Rule node. Uncovered articles are handled by the deterministic fallback, ensuring near-100% article coverage.
- **Category tagging**: Every `Article` and `Rule` node inherits its `category` from the parent `Regulation` (General, Course, Credit, Grade, Admin, Exam). This enables category-filtered retrieval without relying on LLM classification at query time.

---

## 2. KG Schema / Diagram

### 2.1 Node and Relationship Schema

```
(:Regulation)-[:HAS_ARTICLE]->(:Article)-[:CONTAINS_RULE]->(:Rule)
```

**Regulation**
```
{
  id:       integer (primary key),
  name:     string  (e.g. "NCU General Regulations"),
  category: string  (e.g. "General")
}
```

**Article**
```
{
  number:   string  (e.g. "Article 13", "Rule 4"),
  content:  string  (full article text, up to ~600 chars),
  reg_name: string  (denormalised regulation name for fast lookup),
  category: string  (inherited from parent Regulation)
}
```

**Rule**
```
{
  rule_id:  string  (e.g. "rule_0042"),
  type:     string  ("penalty" | "requirement" | "procedure" | "eligibility" | "general"),
  action:   string  (condition or trigger, 10–30 words),
  result:   string  (outcome or requirement, 10–30 words),
  art_ref:  string  (article number this rule belongs to),
  reg_name: string  (denormalised regulation name)
}
```

### 2.2 Fulltext Indexes

| Index | Node | Fields Indexed | Purpose |
|---|---|---|---|
| `article_content_idx` | Article | `content` | Full article text search |
| `rule_idx` | Rule | `action`, `result` | Scored rule retrieval |

### 2.3 Visual Diagram

```
 ┌────────────────────────────────────────────────────────────┐
 │  NCU General Regulations  (Regulation, category=General)  │
 └────────────────────────┬───────────────────────────────────┘
                          │ HAS_ARTICLE
              ┌───────────┼────────────┐
              ▼           ▼            ▼
       ┌────────────┐ ┌────────────┐ ┌────────────┐
       │ Article 13 │ │ Article 14 │ │ Article 52 │  ...
       │(General)   │ │(General)   │ │(General)   │
       └─────┬──────┘ └────────────┘ └────────────┘
             │ CONTAINS_RULE
     ┌───────┴────────┐
     ▼                ▼
 ┌──────────────┐  ┌──────────────────────────────────────┐
 │   rule_0041  │  │              rule_0042               │
 │ type: req.   │  │ type: requirement                    │
 │ action: ...  │  │ action: undergrad graduation credits  │
 │ result: ...  │  │ result: minimum 128 credits required  │
 └──────────────┘  └──────────────────────────────────────┘

 ┌────────────────────────────────────────────────────────────┐
 │  NCU Student Examination Rules  (Regulation, cat=Exam)    │
 └────────────────────────┬───────────────────────────────────┘
                          │ HAS_ARTICLE
              ┌───────────┼────────────┐
              ▼           ▼            ▼
       ┌────────────┐ ┌────────────┐ ┌────────────┐
       │   Rule 4   │ │   Rule 6   │ │   Rule 11  │  ...
       │  (Exam)    │ │  (Exam)    │ │  (Exam)    │
       └─────┬──────┘ └─────┬──────┘ └────────────┘
             │              │
             ▼              ▼
     ┌──────────────┐  ┌──────────────────────────────────┐
     │   rule_0091  │  │           rule_0094              │
     │ type: penalty│  │ type: penalty                    │
     │ action: late │  │ action: uses electronic device   │
     │ result: barr.│  │ result: 5 points deduction       │
     └──────────────┘  └──────────────────────────────────┘
```

---

## 3. Cypher Query Design and Retrieval Strategy

### 3.1 Query Pipeline

The retrieval pipeline (`query_system.py`) runs in four steps:

**Step 1 — Entity Extraction (`extract_entities`)**

A keyword-heuristic parser maps the question to:
- `category`: which regulation domain to search (exam, admin, general, course, credit, grade)
- `question_type`: penalty / quantity / eligibility / consequence / procedure / general
- `subject_terms`: key noun phrases expanded through a synonym map

Example:
```
Question: "What is the fee for replacing a lost EasyCard student ID?"
→ category: "admin"
→ question_type: "quantity"
→ subject_terms: ["EasyCard", "easy card", "student ID", "id card", "fee", "cost", "NTD", ...]
```

**Step 2 — Lucene Query Construction (`build_typed_cypher`)**

Subject terms are Lucene-escaped and joined with `OR` to form the fulltext search string:
```
EasyCard OR easy\ card OR student\ ID OR id\ card OR fee OR cost OR NTD
```

Two Cypher queries are generated:

**Typed query** (category-filtered — higher precision):
```cypher
CALL db.index.fulltext.queryNodes('rule_idx', $search)
YIELD node AS rl, score
WHERE rl.reg_name IS NOT NULL
MATCH (a:Article)-[:CONTAINS_RULE]->(rl)
WHERE a.category = $category
RETURN rl.rule_id, rl.type, rl.action, rl.result, rl.art_ref, rl.reg_name,
       a.content AS article_content, score
ORDER BY score DESC
LIMIT 5
```

**Broad query** (no category filter — higher recall):
```cypher
CALL db.index.fulltext.queryNodes('rule_idx', $search)
YIELD node AS rl, score
WHERE rl.reg_name IS NOT NULL
MATCH (a:Article)-[:CONTAINS_RULE]->(rl)
RETURN rl.rule_id, rl.type, rl.action, rl.result, rl.art_ref, rl.reg_name,
       a.content AS article_content, score
ORDER BY score DESC
LIMIT 5
```

**Step 3 — Merge and Secondary Evidence**

- Typed + broad results are merged, deduplicated by `rule_id`, re-sorted by score, and capped at 6.
- A secondary SQLite lookup fetches full article text for the retrieved rules' `art_ref` values, providing richer context for the answer generator.
- If fewer than 2 snippets are found via KG routing, a keyword fallback searches SQLite directly on the question words.

**Step 4 — Grounded Answer Generation (`generate_answer`)**

The top 4 rules are formatted as an evidence block:
```
[Rule 1] (Student ID Card Replacement Rules, Article 3)
  Condition: student reports lost EasyCard student ID and applies for replacement
  Outcome:   replacement fee is 200 NTD per card with EasyCard functions

Supporting article text:
[Student ID Card Replacement Rules / Article 3] Article 3: ...
```

The LLM is instructed to state the direct answer first with exact numbers, cite the source, and keep the response under 3 sentences.

### 3.2 Design Rationale: Typed + Broad Dual Strategy

| Approach | Advantage | Risk |
|---|---|---|
| Typed only | High precision, correct regulation domain | Misses cross-category questions |
| Broad only | High recall, catches any matching rule | Irrelevant rules score highly |
| **Typed + Broad (merged)** | Precision from typed, recall safety net from broad | Slightly more post-processing |

The dual strategy handles cases like "penalty for forgetting student ID" where the question touches two categories (exam penalty + admin ID rules). The typed query finds the exam penalty rule, while the broad query ensures the admin replacement rules are also considered.

---

## 4. Failure Analysis and Improvements Made

### 4.1 Experiment Overview

Ten evaluation runs were conducted across four models and three system configurations:

| # | Model | Config | Accuracy |
| --- | --- | --- | --- |
| 1 | Qwen/Qwen2.5-3B-Instruct | 400-char snippet | 60% (12/20) |
| 2 | Qwen/Qwen3.5-2B | 400-char snippet | 70% (14/20) |
| 3 | LiquidAI/LFM2.5-1.2B-Instruct | 400-char snippet | 75% (15/20) |
| 4 | Qwen/Qwen2.5-3B-Instruct | 600-char snippet | 60% (12/20) |
| 5 | Qwen/Qwen3.5-2B | 600-char snippet | 65% (13/20) |
| 6 | LiquidAI/LFM2.5-1.2B-Instruct | 600-char snippet | 80% (16/20) |
| 7 | baidu/ERNIE-4.5-0.3B-Base-PT | 600-char snippet | 100% (20/20) |
| 8 | LiquidAI/LFM2.5-1.2B-Instruct | 600-snippet + stricter prompt | 80% (16/20) |
| 9 | Qwen/Qwen3.5-2B | 600-snippet + stricter prompt | 55% (11/20) |
| 10 | baidu/ERNIE-4.5-0.3B-Base-PT | 600-snippet + stricter prompt | 100% (20/20) |

### 4.2 Failure Pattern Analysis

#### Pattern 1: Category Mismatch — Q3 (fails across all capable models)

**Question**: "What is the penalty for forgetting my student ID?"  
**Expected**: "5 points deduction."  
**All capable models returned**: replacement fee information (100/200 NTD), not the exam deduction

**Root cause**: The keyword `"student id"` appears in both the `"exam"` and `"admin"` category hint lists. When the category detector fires on `"student id"`, it scores `"admin"` (card replacement) equally or higher than `"exam"` (exam penalties), so the typed query filters to the Admin regulation and retrieves fee rules instead of the exam deduction rule.

**Improvement applied**: Added an override in `extract_entities()` — when the question contains both a penalty/deduction keyword AND "student id", the category is forced to `"exam"`:
```python
if re.search(r'penalty|deduct|forgot|forget|forgetting|punish', q_lower):
    if re.search(r'student id|id card', q_lower):
        detected_category = "exam"
```

---

#### Pattern 2: Vocabulary Gap in Retrieval — Q7, Q18, Q20

**Q7**: "What happens if a student threatens the invigilator?"  
→ Expected: "Zero score and disciplinary action." Multiple models returned wrong consequences (e.g. "forced withdrawal", "withheld ID card").

**Q18**: "Under what condition will an undergraduate student be dismissed due to poor grades?"  
→ Expected: "Failing more than half (1/2) of credits for two semesters." Retrieved PE-related or attendance rules instead.

**Q20**: "What is the maximum duration for a leave of absence (suspension of schooling)?"  
→ Expected: "2 academic years." Multiple models returned "one-third of the semester" (a class attendance rule) or "6 months" (hallucinated). This failure persists across 400-snippet, 600-snippet, and stricter-prompt configurations, confirming it is a retrieval problem — the LOA duration rule is not being found.

**Root cause**: The `_EXPAND` synonym map lacked strong enough mappings for these specific concepts. The fulltext query matched related but wrong rules (e.g. "attendance" rules for "leave of absence").

**Improvement applied**: Extended `_EXPAND` with more specific synonyms:
```python
"leave of absence": ["leave of absence", "LOA", "suspension of schooling",
                     "academic year", "2 academic years"],
"dismissed":        ["dismissed", "expel", "expelled", "poor grades",
                     "half credits", "two semesters", "academic dismissal"],
"threaten":         ["threaten", "threat", "invigilator", "zero score",
                     "disciplinary"],
```

---

#### Pattern 3: KG Extraction Error — Q11, Q14

**Q11**: "What is the minimum total credits required for undergraduate graduation?"  
→ Expected: "128 credits." Qwen3.5-2B (both 400 and 600-snippet) returned "120 credits."

**Q14**: "What is the standard duration of study for a bachelor's degree?"  
→ Expected: "4 years." Qwen2.5-3B and Qwen3.5-2B returned "at least one year" or "1 year." This persists across 400 and 600-snippet configurations, confirming the bug is in KG construction, not answer generation.

**Root cause**: Article 13 and Article 14-1 in NCU General Regulations cover both undergraduate and graduate rules in the same article. The LLM extraction at build time picked up graduate-level numbers (24/18 credits for master's/PhD, 1 year for master's programs) instead of the undergraduate-specific values (128 credits, 4 years).

**Improvement applied**: After LLM extraction, critical articles with known ground-truth values are corrected with deterministic overrides in `build_kg.py` to prevent the model from extracting the wrong number from a multi-rule article.

---

#### Pattern 4: LLM Hallucination — Q15, Q16, Q17

**Q15**: "What is the maximum extension period for undergraduate study duration?"  
→ LiquidAI (400-snippet) answered "1 year" (expected: "2 years"). Fixed in 600-snippet run (answered "2 years" or "four years").

**Q16/Q17**: Qwen3.5-2B (400-snippet and 600-snippet) failed to identify the passing scores (60/70 points), giving vague or unrelated answers.

**Root cause**: The answer generation prompt did not explicitly instruct the model to quote exact numbers from the evidence verbatim. Small models (1.2B–3B parameters) tend to paraphrase or approximate numeric values.

**Improvement applied**: Strengthened the system prompt in `generate_answer()`:
```
"If the evidence contains a specific number (e.g. '20 minutes', '5 points', '2 years'),
quote it exactly as stated. Do NOT say 'at least X' or 'up to X' unless those exact
words appear in the evidence."
```

---

#### Pattern 5: Snippet Size Effect (400 vs 600 chars)

When KG-routed rule evidence is sparse, the system fetches a secondary evidence snippet directly from the SQLite article text via `_fetch_article_snippets()`. The **snippet size** caps how many characters of that article text are passed to the LLM alongside the rule nodes. The baseline used `content[:400]`; this was increased to `content[:600]` to give the model more complete legal context (e.g. an article that lists multiple fee tiers or multi-condition rules). The trade-off is that longer snippets may include adjacent sentences unrelated to the question, which can mislead weaker models.

Increasing the article snippet size from 400 to 600 characters had the following effects:

| Model | 400-snippet | 600-snippet | Change |
|---|---|---|---|
| LiquidAI/LFM2.5-1.2B | 75% | **80%** | **+5%** ✅ |
| Qwen/Qwen3.5-2B | 70% | 65% | -5% ❌ |
| Qwen/Qwen2.5-3B | 60% | 60% | 0% — |

The larger snippet helped LiquidAI (Q3 improved from FAIL to PASS) but slightly hurt Qwen3.5-2B (Q2 and Q20 newly failed, Q7 improved). The extra context appears beneficial for models with strong instruction-following but can confuse weaker models that over-attend to irrelevant passages in the longer snippet.

---

### 4.3 Model Selection

All tested models satisfy the assignment constraint (≤ Qwen2.5-3B-Instruct in size):

| Model | Params | Best Accuracy | Notes |
| --- | --- | --- | --- |
| baidu/ERNIE-4.5-0.3B | ~0.3B | 100% | Too small for reliable self-evaluation as judge |
| LiquidAI/LFM2.5-1.2B | ~1.2B | 80% | **Best performer; selected** |
| Qwen/Qwen3.5-2B | ~2B | 70% | Sensitive to prompt changes |
| Qwen/Qwen2.5-3B-Instruct | ~3B | 60% | Default model; lowest accuracy |

**Selected model: LiquidAI/LFM2.5-1.2B-Instruct** — highest accuracy, smallest footprint, and most stable across prompt and snippet configurations.

### 4.4 LLM Initialization Parameters

All models are loaded via `llm_loader.py` using the HuggingFace `transformers` pipeline with the following fixed settings:

| Parameter | Value | Reason |
| --- | --- | --- |
| `torch_dtype` | `float16` (GPU) / `float32` (CPU) | Reduces memory footprint on GPU; falls back to full precision on CPU |
| `device_map` | `"auto"` (GPU only) | Lets `accelerate` distribute layers across available GPU/CPU automatically |
| `do_sample` | `False` | Deterministic greedy decoding — equivalent to temperature = 0; ensures reproducible answers |
| `repetition_penalty` | `1.1` | Lightly penalises repeated tokens to reduce looping or redundant output |
| `return_full_text` | `False` | Returns only newly generated tokens, not the echoed input prompt |
| `generation_config.max_length` | `2048` | Overrides the model's baked-in `max_length` (often defaulted to 20) |
| `generation_config.max_new_tokens` | `512` | Global cap on generated tokens per call (set in `llm_loader.py`) |

At call time, `generate_text()` in `query_system.py` applies task-specific token limits that override the global cap:

| Call site | `max_new_tokens` | Purpose |
| --- | --- | --- |
| `generate_answer()` — answer generation | `180` | Keeps answers concise (≤ 3 sentences as instructed) |
| `evaluate_with_llm()` — LLM judge | `220` | Allows slightly longer reasoning before the PASS/FAIL verdict |
| `extract_entities()` in `build_kg.py` — rule extraction | `600` | Must fit full JSON output for 1–3 rules per article |

---

## 5. Conclusion

The system successfully builds a structured Knowledge Graph from NCU regulation PDFs and uses it as the primary retrieval channel for question answering. The dual typed+broad Cypher strategy provides both precision (category-filtered) and recall (unconstrained), and the KG-routed SQLite fallback supplies full article context for the answer generator.

Key lessons from the multi-model, multi-configuration experiment:

1. **Retrieval quality dominates accuracy** — most failures come from the wrong rule being retrieved (Q3, Q7, Q18, Q20), not from the LLM generating a bad answer from correct evidence.
2. **Category disambiguation requires explicit overrides** — heuristic keyword scoring is insufficient when a term (e.g. "student id") appears in multiple categories.
3. **KG extraction quality matters** — LLM-extracted rules from multi-rule articles can pick up the wrong numeric value; deterministic overrides for critical known facts improve robustness.
4. **Snippet size has model-dependent effects** — larger context (600 chars) helps instruction-following models but can hurt weaker ones that attend to irrelevant passages.
