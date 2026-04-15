"""Minimal KG builder template for Assignment 4.

Keep this contract unchanged:
- Graph: (Regulation)-[:HAS_ARTICLE]->(Article)-[:CONTAINS_RULE]->(Rule)
- Article: number, content, reg_name, category
- Rule: rule_id, type, action, result, art_ref, reg_name
- Fulltext indexes: article_content_idx, rule_idx
- SQLite file: ncu_regulations.db
"""

import json
import os
import re
import sqlite3
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
    os.getenv("NEO4J_USER", "neo4j"),
    os.getenv("NEO4J_PASSWORD", "password"),
)


def extract_entities(article_number: str, reg_name: str, content: str) -> dict[str, Any]:
    """Use LLM to extract structured rules from article content.

    Returns {"rules": [{"type": ..., "action": ..., "result": ...}, ...]}
    """
    if not content or len(content.strip()) < 30:
        return {"rules": []}

    tok = get_tokenizer()
    pipe = get_raw_pipeline()
    if tok is None or pipe is None:
        load_local_llm()
        tok = get_tokenizer()
        pipe = get_raw_pipeline()

    snippet = content[:600]

    messages = [
        {
            "role": "system",
            "content": (
                "You are a regulation parser. Extract rules from regulation text as JSON.\n"
                'Output format: {"rules": [{"type": "penalty|requirement|procedure|eligibility", '
                '"action": "specific condition or trigger (10-30 words)", '
                '"result": "specific outcome or requirement (10-30 words)"}]}\n'
                "Rules for extraction:\n"
                "- Extract 1-3 most important, distinct rules.\n"
                "- Use exact numbers and facts from the text (e.g. '20 minutes', '5 points', '200 NTD').\n"
                "- If no clear rule exists, return {\"rules\": []}.\n"
                "- Output JSON only, no explanation or markdown."
            ),
        },
        {
            "role": "user",
            "content": f"{article_number} ({reg_name}):\n{snippet}",
        },
    ]

    try:
        prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        raw = pipe(prompt, max_new_tokens=600)[0]["generated_text"].strip()

        valid = []

        # Strategy 1: parse the full {"rules": [...]} structure
        try:
            json_match = re.search(r'\{[\s\S]*?"rules"[\s\S]*?\}', raw)
            if json_match:
                data = json.loads(json_match.group())
                for r in data.get("rules", []):
                    if not isinstance(r, dict):
                        continue
                    action = str(r.get("action", "")).strip()
                    result = str(r.get("result", "")).strip()
                    if action and result and len(action) > 5 and len(result) > 5:
                        valid.append({
                            "type": str(r.get("type", "general")).strip(),
                            "action": action[:300],
                            "result": result[:300],
                        })
        except (json.JSONDecodeError, AttributeError):
            pass

        # Strategy 2: extract individual rule objects when full JSON is truncated
        if not valid:
            for obj_match in re.finditer(r'\{[^{}]*?"action"[^{}]*?"result"[^{}]*?\}', raw):
                try:
                    r = json.loads(obj_match.group())
                    action = str(r.get("action", "")).strip()
                    result = str(r.get("result", "")).strip()
                    if action and result and len(action) > 5 and len(result) > 5:
                        valid.append({
                            "type": str(r.get("type", "general")).strip(),
                            "action": action[:300],
                            "result": result[:300],
                        })
                except json.JSONDecodeError:
                    continue

        # Strategy 3: regex-extract key-value pairs from malformed JSON
        if not valid:
            action_m = re.search(r'"action"\s*:\s*"([^"]{10,})"', raw)
            result_m = re.search(r'"result"\s*:\s*"([^"]{10,})"', raw)
            type_m   = re.search(r'"type"\s*:\s*"([^"]+)"', raw)
            if action_m and result_m:
                valid.append({
                    "type": type_m.group(1) if type_m else "general",
                    "action": action_m.group(1)[:300],
                    "result": result_m.group(1)[:300],
                })

        if valid:
            return {"rules": valid}

    except Exception as e:
        print(f"   ⚠ LLM extraction failed for {article_number}: {e}")

    return {"rules": []}


def build_fallback_rules(article_number: str, content: str) -> list[dict[str, str]]:
    """Deterministic fallback: build rules from sentence-pair patterns when LLM fails.

    Strategy:
    - Split content into sentences.
    - For sentences containing key legal facts (numbers, penalties, requirements),
      pair the preceding sentence (context/action) with the fact sentence (result).
    - If nothing found, create one general rule from the full content.
    """
    if not content or len(content.strip()) < 20:
        return []

    # Split into sentences
    sentences = [s.strip() for s in re.split(r'(?<=[.;])\s+', content) if len(s.strip()) > 15]
    if not sentences:
        return [{"type": "general", "action": article_number, "result": content[:300]}]

    # Patterns that indicate a legally significant sentence
    significant = re.compile(
        r'\b(\d+)\s*(point[s]?|credit[s]?|year[s]?|semester[s]?|day[s]?|minute[s]?|hour[s]?|NTD|NT\$)\b'
        r'|zero\s*score|deduction|penalty|dismiss|expel|disciplinary'
        r'|must|shall|required|minimum|at\s*least|not\s*allowed|prohibited|barred|forbidden',
        re.IGNORECASE,
    )

    # Determine the type from content keywords
    def infer_type(text: str) -> str:
        t = text.lower()
        if re.search(r'zero\s*score|deduction|penalty|dismiss|expel|disciplinary|barred', t):
            return "penalty"
        if re.search(r'must|shall|required|minimum|at\s*least', t):
            return "requirement"
        if re.search(r'procedure|process|application|submit|apply', t):
            return "procedure"
        return "general"

    rules: list[dict[str, str]] = []
    seen: set[str] = set()

    for i, sent in enumerate(sentences):
        if not significant.search(sent):
            continue
        # Use the previous sentence as action context, current as result
        action = sentences[i - 1] if i > 0 else article_number
        result = sent
        key = result[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        rules.append({
            "type": infer_type(result),
            "action": action[:250],
            "result": result[:250],
        })
        if len(rules) >= 3:
            break

    # Generic fallback: turn adjacent sentence pairs into rules to maximise fulltext coverage
    if not rules:
        for i in range(min(len(sentences) - 1, 2)):
            rules.append({
                "type": "general",
                "action": sentences[i][:250],
                "result": sentences[i + 1][:250],
            })
        if not rules:
            rules.append({"type": "general", "action": article_number, "result": content[:300]})

    return rules


# SQLite tables used:
# - regulations(reg_id, name, category)
# - articles(reg_id, article_number, content)


def build_graph() -> None:
    """Build KG from SQLite into Neo4j using the fixed assignment schema."""
    sql_conn = sqlite3.connect("ncu_regulations.db")
    cursor = sql_conn.cursor()
    driver = GraphDatabase.driver(URI, auth=AUTH)

    # Optional: warm up local LLM
    load_local_llm()

    with driver.session() as session:
        # Fixed strategy: clear existing graph data before rebuilding.
        session.run("MATCH (n) DETACH DELETE n")

        # 1) Read regulations and create Regulation nodes.
        cursor.execute("SELECT reg_id, name, category FROM regulations")
        regulations = cursor.fetchall()
        reg_map: dict[int, tuple[str, str]] = {}

        for reg_id, name, category in regulations:
            reg_map[reg_id] = (name, category)
            session.run(
                "MERGE (r:Regulation {id:$rid}) SET r.name=$name, r.category=$cat",
                rid=reg_id,
                name=name,
                cat=category,
            )

        # 2) Read articles and create Article + HAS_ARTICLE.
        cursor.execute("SELECT reg_id, article_number, content FROM articles")
        articles = cursor.fetchall()

        for reg_id, article_number, content in articles:
            reg_name, reg_category = reg_map.get(reg_id, ("Unknown", "Unknown"))
            session.run(
                """
                MATCH (r:Regulation {id: $rid})
                CREATE (a:Article {
                    number:   $num,
                    content:  $content,
                    reg_name: $reg_name,
                    category: $reg_category
                })
                MERGE (r)-[:HAS_ARTICLE]->(a)
                """,
                rid=reg_id,
                num=article_number,
                content=content,
                reg_name=reg_name,
                reg_category=reg_category,
            )

        # 3) Create full-text index on Article content.
        session.run(
            """
            CREATE FULLTEXT INDEX article_content_idx IF NOT EXISTS
            FOR (a:Article) ON EACH [a.content]
            """
        )

        rule_counter = 0

        # 3) Extract and create Rule nodes for each article.
        # Deduplication key: normalised (action[:80], result[:80]) pair per regulation.
        seen_rules: set[tuple[str, str]] = set()

        for reg_id, article_number, content in articles:
            reg_name, reg_category = reg_map.get(reg_id, ("Unknown", "Unknown"))

            # Primary: LLM extraction
            extracted = extract_entities(article_number, reg_name, content)
            rules = extracted.get("rules", [])

            # Fallback: deterministic patterns when LLM yields nothing
            if not rules:
                rules = build_fallback_rules(article_number, content)

            for rule in rules:
                action = str(rule.get("action", "")).strip()
                result = str(rule.get("result", "")).strip()
                rule_type = str(rule.get("type", "general")).strip()

                # Skip rules without meaningful content
                if not action or not result or len(action) < 5 or len(result) < 5:
                    continue

                # Deduplicate within the same regulation
                dedup_key = (reg_name, action[:80].lower(), result[:80].lower())
                if dedup_key in seen_rules:
                    continue
                seen_rules.add(dedup_key)

                rule_counter += 1
                rule_id = f"rule_{rule_counter:04d}"

                session.run(
                    """
                    MATCH (a:Article {number: $num, reg_name: $reg_name})
                    CREATE (rl:Rule {
                        rule_id:  $rule_id,
                        type:     $type,
                        action:   $action,
                        result:   $result,
                        art_ref:  $art_ref,
                        reg_name: $reg_name
                    })
                    CREATE (a)-[:CONTAINS_RULE]->(rl)
                    """,
                    num=article_number,
                    reg_name=reg_name,
                    rule_id=rule_id,
                    type=rule_type,
                    action=action,
                    result=result,
                    art_ref=article_number,
                )

            print(f"   [{reg_name}] {article_number}: {len(rules)} rule(s) extracted")

        # 4) Create full-text index on Rule fields.
        session.run(
            """
            CREATE FULLTEXT INDEX rule_idx IF NOT EXISTS
            FOR (r:Rule) ON EACH [r.action, r.result]
            """
        )

        # 5) Coverage audit (provided scaffold).
        coverage = session.run(
            """
            MATCH (a:Article)
            OPTIONAL MATCH (a)-[:CONTAINS_RULE]->(r:Rule)
            WITH a, count(r) AS rule_count
            RETURN count(a) AS total_articles,
                   sum(CASE WHEN rule_count > 0 THEN 1 ELSE 0 END) AS covered_articles,
                   sum(CASE WHEN rule_count = 0 THEN 1 ELSE 0 END) AS uncovered_articles
            """
        ).single()

        total_articles = int((coverage or {}).get("total_articles", 0) or 0)
        covered_articles = int((coverage or {}).get("covered_articles", 0) or 0)
        uncovered_articles = int((coverage or {}).get("uncovered_articles", 0) or 0)

        print(
            f"[Coverage] covered={covered_articles}/{total_articles}, "
            f"uncovered={uncovered_articles}"
        )

    driver.close()
    sql_conn.close()


if __name__ == "__main__":
    build_graph()
