"""KG query system for Assignment 4.

Keep these APIs unchanged for auto-test:
- generate_text(messages, max_new_tokens=220)
- get_relevant_articles(question)
- generate_answer(question, rule_results)

Keep Rule fields aligned with build_kg output:
rule_id, type, action, result, art_ref, reg_name
"""

import os
import re
import sqlite3
from typing import Any

from neo4j import GraphDatabase
from dotenv import load_dotenv

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
    os.getenv("NEO4J_USER", "neo4j"),
    os.getenv("NEO4J_PASSWORD", "password"),
)

# Avoid local proxy settings interfering with model/Neo4j access.
for key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    if key in os.environ:
        del os.environ[key]


try:
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
except Exception as e:
    print(f"⚠️ Neo4j connection warning: {e}")
    driver = None


# ========== 1) Public API (query flow order) ==========
# Order: extract_entities -> build_typed_cypher -> get_relevant_articles -> generate_answer

def generate_text(messages: list[dict[str, str]], max_new_tokens: int = 220) -> str:
    """Call local HF model via chat template + raw pipeline."""
    tok = get_tokenizer()
    pipe = get_raw_pipeline()
    if tok is None or pipe is None:
        load_local_llm()
        tok = get_tokenizer()
        pipe = get_raw_pipeline()
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return pipe(prompt, max_new_tokens=max_new_tokens)[0]["generated_text"].strip()


# ── Keyword expansion maps for common question domains ──────────────────────

_CATEGORY_HINTS = {
    "exam":    ["exam", "examination", "invigilator", "cheat", "late", "barred", "leave",
                "question paper", "student id", "electronic", "device", "threaten", "score",
                "deduction", "minutes", "forbidden", "exam room"],
    "admin":   ["student id", "id card", "easycard", "mifare", "replace", "lost", "fee",
                "NTD", "working day", "application"],
    "general": ["graduation", "credit", "PE", "physical education", "military", "semester",
                "bachelor", "year", "study", "extension", "passing", "score", "grade",
                "dismissed", "expel", "make-up", "leave of absence", "suspension"],
    "course":  ["course", "selection", "enroll", "drop", "add", "withdraw"],
    "credit":  ["transfer", "credit transfer", "recognition"],
    "grade":   ["grading", "grade", "GPA", "transcript"],
}

_EXPAND = {
    "student id":      ["student ID", "id card", "identification"],
    "easycard":        ["EasyCard", "easy card"],
    "mifare":          ["Mifare", "non-EasyCard"],
    "late":            ["late", "tardy", "barred", "not allowed to enter"],
    "leave":           ["leave the exam", "exit"],
    "cheat":           ["cheat", "cheating", "copy", "pass notes", "plagiar"],
    "electronic":      ["electronic device", "communication device", "phone", "mobile"],
    "threaten":        ["threaten", "threat", "invigilator"],
    "graduation":      ["graduation", "graduate", "graduate requirement"],
    "credit":          ["credit", "credits", "credit hour"],
    "pe":              ["Physical Education", "PE", "physical education"],
    "military":        ["Military Training", "military training"],
    "passing":         ["passing score", "pass", "minimum score"],
    "dismissed":       ["dismissed", "dismiss", "expel", "expelled", "academic dismissal"],
    "leave of absence":["leave of absence", "suspension", "LOA"],
    "make-up":         ["make-up exam", "makeup", "supplemental exam"],
    "replace":         ["replace", "replacement", "reissue", "lost"],
    "fee":             ["fee", "cost", "charge", "NTD"],
}


def _expand_terms(terms: list[str]) -> list[str]:
    """Expand query terms using synonym map."""
    expanded = list(terms)
    for t in terms:
        t_lower = t.lower()
        for key, syns in _EXPAND.items():
            if key in t_lower or t_lower in key:
                expanded.extend(syns)
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in expanded:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def extract_entities(question: str) -> dict[str, Any]:
    """Parse question to {question_type, subject_terms, aspect, category}.

    Uses keyword heuristics first (fast, reliable for known domains), then
    falls back to LLM if heuristic coverage is weak.
    """
    q_lower = question.lower()

    # ── Heuristic: detect category ──────────────────────────────────────────
    detected_category = "general"
    best_hits = 0
    for cat, hints in _CATEGORY_HINTS.items():
        hits = sum(1 for h in hints if h.lower() in q_lower)
        if hits > best_hits:
            best_hits = hits
            detected_category = cat

    # ── Heuristic: question type ─────────────────────────────────────────────
    if re.search(r'\bpenalt|deduction|punish|consequence|fine\b', q_lower):
        q_type = "penalty"
    elif re.search(r'\bhow many|how long|how much|duration|maximum|minimum|number of\b', q_lower):
        q_type = "quantity"
    elif re.search(r'\bcan i|allowed|permitted|is it possible|may i\b', q_lower):
        q_type = "eligibility"
    elif re.search(r'\bwhat happen|what if|result|consequence\b', q_lower):
        q_type = "consequence"
    elif re.search(r'\bhow to|procedure|process|steps|apply\b', q_lower):
        q_type = "procedure"
    else:
        q_type = "general"

    # ── Heuristic: extract key noun phrases as subject terms ─────────────────
    # Remove question words and stop words, keep meaningful tokens
    stop = {"what", "when", "where", "which", "who", "how", "is", "are", "the",
            "a", "an", "of", "for", "in", "to", "do", "does", "can", "i",
            "student", "my", "be", "if", "this"}
    tokens = re.findall(r"[a-zA-Z0-9\-']+", question)
    subject_terms = [t for t in tokens if t.lower() not in stop and len(t) > 2]

    # Also add category-matched hints as auxiliary terms
    if detected_category in _CATEGORY_HINTS:
        for hint in _CATEGORY_HINTS[detected_category]:
            if hint.lower() in q_lower and hint not in subject_terms:
                subject_terms.insert(0, hint)

    subject_terms = _expand_terms(subject_terms[:8])

    return {
        "question_type": q_type,
        "subject_terms": subject_terms[:12],
        "aspect": detected_category,
        "category": detected_category,
    }


def _lucene_escape(term: str) -> str:
    """Escape special Lucene characters for Neo4j fulltext queries."""
    special = r'+-&&||!(){}[]^"~*?:\/'
    return "".join(f"\\{c}" if c in special else c for c in term)


def build_typed_cypher(entities: dict[str, Any]) -> tuple[str, str]:
    """Return (typed_query, broad_query) as Cypher strings with $query param.

    typed_query  – uses category filter + scored fulltext on rule_idx.
    broad_query  – broader fulltext on rule_idx without category filter,
                   catches cross-category or loosely worded questions.
    """
    terms = entities.get("subject_terms", [])
    category = entities.get("category", "")

    # Build a Lucene query string from the subject terms
    # Primary: exact and OR-combined terms
    if terms:
        escaped = [_lucene_escape(t) for t in terms[:6]]
        # Combine: first two terms as phrase-like, rest as OR
        lucene_terms = " OR ".join(escaped)
    else:
        lucene_terms = "*"

    # Category label mapping to reg_name / category field
    _cat_map = {
        "exam":    "Exam",
        "admin":   "Admin",
        "general": "General",
        "course":  "Course",
        "credit":  "Credit",
        "grade":   "Grade",
    }
    cat_label = _cat_map.get(category, "")

    # Typed query: filter by category, scored fulltext
    # NOTE: use $search (not $query) to avoid collision with session.run(query=...)
    if cat_label:
        cypher_typed = """
CALL db.index.fulltext.queryNodes('rule_idx', $search)
YIELD node AS rl, score
WHERE rl.reg_name IS NOT NULL
MATCH (a:Article)-[:CONTAINS_RULE]->(rl)
WHERE a.category = $category
RETURN rl.rule_id   AS rule_id,
       rl.type      AS type,
       rl.action    AS action,
       rl.result    AS result,
       rl.art_ref   AS art_ref,
       rl.reg_name  AS reg_name,
       a.content    AS article_content,
       score
ORDER BY score DESC
LIMIT 5
"""
    else:
        cypher_typed = """
CALL db.index.fulltext.queryNodes('rule_idx', $search)
YIELD node AS rl, score
WHERE rl.reg_name IS NOT NULL
MATCH (a:Article)-[:CONTAINS_RULE]->(rl)
RETURN rl.rule_id   AS rule_id,
       rl.type      AS type,
       rl.action    AS action,
       rl.result    AS result,
       rl.art_ref   AS art_ref,
       rl.reg_name  AS reg_name,
       a.content    AS article_content,
       score
ORDER BY score DESC
LIMIT 5
"""

    # Broad query: no category filter, looser term set
    cypher_broad = """
CALL db.index.fulltext.queryNodes('rule_idx', $search)
YIELD node AS rl, score
WHERE rl.reg_name IS NOT NULL
MATCH (a:Article)-[:CONTAINS_RULE]->(rl)
RETURN rl.rule_id   AS rule_id,
       rl.type      AS type,
       rl.action    AS action,
       rl.result    AS result,
       rl.art_ref   AS art_ref,
       rl.reg_name  AS reg_name,
       a.content    AS article_content,
       score
ORDER BY score DESC
LIMIT 5
"""

    return cypher_typed, cypher_broad, lucene_terms, cat_label


def _run_query(session: Any, cypher: str, lucene_terms: str, category: str = "") -> list[dict]:
    """Run a Cypher fulltext query and return list of result dicts."""
    try:
        # Use "search" not "query" — neo4j session.run() reserves "query" as its own param name
        params: dict[str, Any] = {"search": lucene_terms}
        if category:
            params["category"] = category
        result = session.run(cypher, **params)
        rows = []
        for record in result:
            rows.append({
                "rule_id":        record.get("rule_id", ""),
                "type":           record.get("type", ""),
                "action":         record.get("action", ""),
                "result":         record.get("result", ""),
                "art_ref":        record.get("art_ref", ""),
                "reg_name":       record.get("reg_name", ""),
                "article_content": record.get("article_content", ""),
                "score":          record.get("score", 0.0),
            })
        return rows
    except Exception as e:
        print(f"   ⚠ Query error: {e}")
        return []


def _fetch_article_snippets(question: str, rule_results: list[dict]) -> list[str]:
    """Secondary evidence: pull full article content from SQLite for retrieved rules.

    KG-routed: only fetch articles that are referenced by KG results.
    Falls back to DB keyword search when KG results are sparse.
    """
    snippets: list[str] = []
    try:
        conn = sqlite3.connect("ncu_regulations.db")
        cursor = conn.cursor()

        # 1) KG-routed: fetch articles linked to retrieved Rule nodes
        art_refs = list({r["art_ref"] for r in rule_results if r.get("art_ref")})
        reg_names = list({r["reg_name"] for r in rule_results if r.get("reg_name")})

        if art_refs and reg_names:
            placeholders_art = ",".join("?" * len(art_refs))
            placeholders_reg = ",".join("?" * len(reg_names))
            cursor.execute(
                f"""
                SELECT a.article_number, a.content, r.name
                FROM articles a
                JOIN regulations r ON a.reg_id = r.reg_id
                WHERE a.article_number IN ({placeholders_art})
                  AND r.name IN ({placeholders_reg})
                LIMIT 4
                """,
                art_refs + reg_names,
            )
            for art_num, content, reg_name in cursor.fetchall():
                snippets.append(f"[{reg_name} / {art_num}] {content[:600]}")

        # 2) DB keyword fallback when KG results are sparse
        if len(snippets) < 2:
            # Extract meaningful words from the question
            stop = {"what", "when", "where", "which", "who", "how", "is", "are", "the",
                    "a", "an", "of", "for", "in", "to", "do", "does", "can", "i", "my", "be"}
            kw_tokens = [t for t in re.findall(r"[a-zA-Z]{4,}", question.lower()) if t not in stop]
            for kw in kw_tokens[:3]:
                cursor.execute(
                    """
                    SELECT a.article_number, a.content, r.name
                    FROM articles a
                    JOIN regulations r ON a.reg_id = r.reg_id
                    WHERE a.content LIKE ?
                    LIMIT 2
                    """,
                    (f"%{kw}%",),
                )
                for art_num, content, reg_name in cursor.fetchall():
                    entry = f"[{reg_name} / {art_num}] {content[:600]}"
                    if entry not in snippets:
                        snippets.append(entry)
                if len(snippets) >= 4:
                    break

        conn.close()
    except Exception as e:
        print(f"   ⚠ SQLite snippet fetch failed: {e}")

    return snippets[:4]


def get_relevant_articles(question: str) -> list[dict[str, Any]]:
    """Run typed + broad retrieval against Neo4j and return merged rule dicts."""
    if driver is None:
        return []

    entities = extract_entities(question)
    typed_cypher, broad_cypher, lucene_terms, cat_label = build_typed_cypher(entities)

    with driver.session() as session:
        # Run typed query first
        typed_results = _run_query(session, typed_cypher, lucene_terms, cat_label)

        # Run broad query; merge results, deduplicate by rule_id
        broad_results = _run_query(session, broad_cypher, lucene_terms)

    seen_ids: set[str] = set()
    merged: list[dict] = []

    for row in typed_results + broad_results:
        rid = row.get("rule_id", "")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            merged.append(row)

    # Sort by descending score, keep top 6
    merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    merged = merged[:6]

    # Attach DB article snippets as secondary evidence channel
    snippets = _fetch_article_snippets(question, merged)
    for row in merged:
        row["_article_snippets"] = snippets

    return merged


def generate_answer(question: str, rule_results: list[dict[str, Any]]) -> str:
    """Generate a grounded answer from retrieved rules only."""
    if not rule_results:
        return "Insufficient rule evidence to answer this question."

    # Build evidence block from rule action/result pairs
    evidence_lines = []
    for i, r in enumerate(rule_results[:4], 1):
        action = r.get("action", "").strip()
        result = r.get("result", "").strip()
        art_ref = r.get("art_ref", "")
        reg_name = r.get("reg_name", "")
        if action and result:
            evidence_lines.append(
                f"[Rule {i}] ({reg_name}, {art_ref})\n"
                f"  Condition: {action}\n"
                f"  Outcome:   {result}"
            )

    # Add DB article snippets if available (secondary channel)
    snippets = rule_results[0].get("_article_snippets", [])
    snippet_block = ""
    if snippets:
        snippet_block = "\n\nSupporting article text:\n" + "\n".join(snippets[:2])

    if not evidence_lines:
        return "Insufficient rule evidence to answer this question."

    evidence_text = "\n\n".join(evidence_lines) + snippet_block

    messages = [
        {
            "role": "system",
            # "content": (
            #     "You are an NCU regulation assistant. Answer strictly based on the provided rule evidence.\n"
            #     "Rules:\n"
            #     "1. State the direct answer first (1 sentence), using exact numbers/facts from the evidence.\n"
            #     "2. Cite the source (regulation name and article) in parentheses.\n"
            #     "3. Do NOT invent facts not present in the evidence.\n"
            #     "4. Keep your answer under 3 sentences."
            # ),
            "content": (
                "You are an NCU regulation assistant. Answer strictly based on the provided rule evidence.\n"
                "Rules:\n"
                "1. State the direct answer first (1 sentence), quoting EXACT numbers/values from the evidence.\n"
                "2. If the evidence contains a number (e.g. '20 minutes', '5 points', '128 credits'), quote it verbatim.\n"
                "3. Do NOT say 'at least' or 'up to' unless those words appear in the evidence.\n"
                "4. Cite the source (regulation name and article) in parentheses.\n"
                "5. Do NOT invent facts. Keep answer under 3 sentences."
            ),

        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Evidence:\n{evidence_text}\n\n"
                "Answer:"
            ),
        },
    ]

    try:
        answer = generate_text(messages, max_new_tokens=180)
        # Strip any repeated "Answer:" prefix the model may echo
        answer = re.sub(r'^(Answer:\s*)+', '', answer, flags=re.IGNORECASE).strip()
        return answer if answer else "Insufficient rule evidence to answer this question."
    except Exception as e:
        return f"Error generating answer: {e}"


def main() -> None:
    """Interactive CLI."""
    if driver is None:
        return

    load_local_llm()

    print("=" * 50)
    print("🎓 NCU Regulation Assistant")
    print("=" * 50)
    print("💡 Try: 'What is the penalty for forgetting student ID?'")
    print("👉 Type 'exit' to quit.\n")

    while True:
        try:
            user_q = input("\nUser: ").strip()
            if not user_q:
                continue
            if user_q.lower() in {"exit", "quit"}:
                print("👋 Bye!")
                break

            results = get_relevant_articles(user_q)
            answer = generate_answer(user_q, results)
            print(f"Bot: {answer}")

        except KeyboardInterrupt:
            print("\n👋 Bye!")
            break
        except Exception as e:
            print(f"❌ Error: {e}")

    driver.close()


if __name__ == "__main__":
    main()
