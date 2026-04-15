# NCU Regulation Q&A System (Assignment 4)

A Knowledge Graph-based Question Answering system for NCU academic regulations.  
The system parses regulation PDFs, builds a Neo4j Knowledge Graph, and answers student questions with evidence-grounded responses via a local LLM.

---

## KG Schema Design

### Graph Structure

```text
(:Regulation)-[:HAS_ARTICLE]->(:Article)-[:CONTAINS_RULE]->(:Rule)
```

### Node Properties

| Node | Properties |
| --- | --- |
| `Regulation` | `id`, `name`, `category` |
| `Article` | `number`, `content`, `reg_name`, `category` |
| `Rule` | `rule_id`, `type`, `action`, `result`, `art_ref`, `reg_name` |

### Relationship Types

| Relationship | From → To | Meaning |
| --- | --- | --- |
| `HAS_ARTICLE` | Regulation → Article | A regulation contains articles |
| `CONTAINS_RULE` | Article → Rule | An article contains extracted rule facts |

### Rule Types

Each `Rule` node is tagged with one of four types extracted from the article content:

- **penalty** — consequences for violations (e.g. score deduction, dismissal)
- **requirement** — minimum thresholds that must be met (e.g. minimum credits)
- **procedure** — step-by-step processes (e.g. ID replacement application)
- **eligibility** — conditions determining whether an action is permitted

### Fulltext Indexes

| Index Name | Target | Fields |
| --- | --- | --- |
| `article_content_idx` | Article nodes | `content` |
| `rule_idx` | Rule nodes | `action`, `result` |

### Schema Diagram

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Neo4j Knowledge Graph                    │
│                                                                 │
│  ┌──────────────┐  HAS_ARTICLE  ┌──────────────┐               │
│  │  Regulation  │──────────────▶│   Article    │               │
│  │              │               │              │               │
│  │ id           │               │ number       │               │
│  │ name         │               │ content      │               │
│  │ category     │               │ reg_name     │               │
│  └──────────────┘               │ category     │               │
│                                 └──────┬───────┘               │
│                                        │ CONTAINS_RULE          │
│                                        ▼                        │
│                                 ┌──────────────┐               │
│                                 │    Rule      │               │
│                                 │              │               │
│                                 │ rule_id      │               │
│                                 │ type         │               │
│                                 │ action       │               │
│                                 │ result       │               │
│                                 │ art_ref      │               │
│                                 │ reg_name     │               │
│                                 └──────────────┘               │
└─────────────────────────────────────────────────────────────────┘
```

### Regulation Sources

| File | Regulation Name | Category |
| --- | --- | --- |
| ncu1.pdf | NCU General Regulations | General |
| ncu2.pdf | Course Selection Regulations | Course |
| ncu3.pdf | Credit Transfer Regulations | Credit |
| ncu4.pdf | Grading System Guidelines | Grade |
| ncu5.pdf | Student ID Card Replacement Rules | Admin |
| ncu6.pdf | NCU Student Examination Rules | Exam |

---

## Neo4j Graph Screenshots

### Graph Overview — Regulation → Article → Rule Chain

> **Screenshot:** Open Neo4j Browser at `http://localhost:7474` and run:
>
> ```cypher
> MATCH path = (reg:Regulation)-[:HAS_ARTICLE]->(a:Article)-[:CONTAINS_RULE]->(r:Rule)
> RETURN path LIMIT 30
> ```

![Full graph overview — Regulation → Article → Rule chain](<images/Screenshot from 2026-04-12 00-19-14.png>)

### Regulation Nodes

> **Screenshot:** Run in Neo4j Browser:
>
> ```cypher
> MATCH (r:Regulation) RETURN r
> ```

![All 6 Regulation nodes](<images/Screenshot from 2026-04-12 00-20-22.png>)

### Article → Rule Detail (Exam Category)

> **Screenshot:** Run in Neo4j Browser:
>
> ```cypher
> MATCH path = (a:Article {category:"Exam"})-[:CONTAINS_RULE]->(r:Rule)
> RETURN path LIMIT 20
> ```

![Article → Rule relationships for Exam category](<images/Screenshot from 2026-04-12 00-21-07.png>)

---

## Query Flow

```text
User Question
     │
     ▼
extract_entities()        ← keyword heuristics → {category, question_type, subject_terms}
     │
     ▼
build_typed_cypher()      ← Lucene query string + category filter
     │
     ├──▶ Typed Cypher (category-filtered fulltext on rule_idx)
     └──▶ Broad Cypher  (no category filter, wider recall)
                │
                ▼
         Merge & Rank     ← deduplicate by rule_id, sort by score, top 6
                │
                ▼
    _fetch_article_snippets()  ← KG-routed SQLite lookup for full article text
                │
                ▼
        generate_answer()      ← LLM prompt with rule evidence + article snippets
                │
                ▼
          Final Answer
```

---

## 🛠️ Prerequisites

- Python 3.11
- Docker Desktop
- Internet access for first-time HuggingFace model download (cached locally after)

---

## ⚙️ Environment Setup

### 1. Start Neo4j (Docker)

```bash
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:latest
```

Verify: open `http://localhost:7474` — login with `neo4j` / `password`.

### 2. Create Virtual Environment

```bash
# macOS / Linux
python -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 🚀 Execution Order

```bash
# 1. Parse PDFs and populate SQLite (skip if ncu_regulations.db already exists)
python setup_data.py

# 2. Build the Knowledge Graph in Neo4j
python build_kg.py

# 3. (Optional) Interactive manual test
python query_system.py

# 4. Run the automated benchmark
python auto_test.py
```

Run all commands from the repository root directory.

---

## 📂 File Descriptions

| File | Purpose |
| --- | --- |
| `setup_data.py` | Parses PDFs with pdfplumber + regex, stores structured rows into SQLite |
| `build_kg.py` | Reads SQLite, uses LLM to extract rule facts, builds Neo4j KG |
| `query_system.py` | Typed + broad KG retrieval, DB article snippets, LLM answer generation |
| `auto_test.py` | LLM-as-judge benchmark against `test_data.json` |
| `llm_loader.py` | Local HuggingFace model loader (singleton, CPU/GPU auto-detect) |
| `source/` | Raw NCU regulation PDFs (ncu1–ncu6) |
| `test_data.json` | 20 benchmark Q&A pairs covering all regulation categories |

---

## Benchmark Results

Multiple models and system configurations were evaluated. Reported accuracy reflects the LLM-as-judge score; estimated true accuracy corrects for known judge failures (see [REPORT.md](REPORT.md) §4.2 Pattern 5).

**Config legend:**

- **400/600-char snippet** — maximum character length of the article text snippet fetched from SQLite and appended to the rule evidence before answer generation. A larger snippet gives the LLM more full-article context but may introduce irrelevant text for weaker models.
- **stricter prompt** — answer generation prompt that explicitly requires the model to quote exact numbers verbatim from the evidence (e.g. "5 points", "2 years") and prohibits paraphrasing numeric values.

| Model | Config | Reported | Est. True |
| --- | --- | --- | --- |
| baidu/ERNIE-4.5-0.3B-Base-PT | 600-char snippet | 100% | ~35% ⚠️ judge broken |
| baidu/ERNIE-4.5-0.3B-Base-PT | 600-snippet + stricter prompt | 100% | ~35% ⚠️ judge broken |
| **LiquidAI/LFM2.5-1.2B-Instruct** | **600-char snippet** | **80%** | **~65%** ✅ selected |
| LiquidAI/LFM2.5-1.2B-Instruct | 600-snippet + stricter prompt | 80% | ~65% |
| LiquidAI/LFM2.5-1.2B-Instruct | 400-char snippet (baseline) | 75% | ~60% |
| Qwen/Qwen3.5-2B | 400-char snippet (baseline) | 70% | ~65% |
| Qwen/Qwen3.5-2B | 600-char snippet | 65% | ~60% |
| Qwen/Qwen3.5-2B | 600-snippet + stricter prompt | 55% | ~50% |
| Qwen/Qwen2.5-3B-Instruct | 400-char snippet (baseline) | 60% | ~55% |
| Qwen/Qwen2.5-3B-Instruct | 600-char snippet | 60% | ~55% |

Current active model: **LiquidAI/LFM2.5-1.2B-Instruct** with 600-char snippet (set in `llm_loader.py`)
