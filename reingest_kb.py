"""
Re-ingest the RAG knowledge base from knowledge/upay_dps.md (the upay MTB Islamic DPS page).

Clears ALL existing documents, then uploads one chunk per "## " section so each FAQ answer is retrieved on its own.
Run INSIDE the api container:
    docker compose exec api python reingest_kb.py
Then restart api and sip-bridge (workers cache the BM25 index):
    docker compose restart api sip-bridge
Refresh the source text first with:  python3 fetch_upay_kb.py   (on the host; rebuild images afterwards)
"""
import re
import sys
import time

sys.path.append('.')

from agent.rag import _vectorstore, add_document, list_documents, search_documents
from agent.upay import load_facts

print("=" * 60)
print("Step 1: Clearing existing ChromaDB documents...")
print("=" * 60)
existing = list_documents()
print(f"Found {len(existing)} existing documents/chunks.")
for doc in existing:
    try:
        _vectorstore.delete(ids=[doc["id"]])
    except Exception as e:
        print(f"  Warning: Could not delete {doc['id']}: {e}")
print(f"Remaining after cleanup: {len(list_documents())}")

print("\n" + "=" * 60)
print("Step 2: Ingesting knowledge/upay_dps.md ...")
print("=" * 60)
text = load_facts()
title_line, _, body = text.partition("\n")
overview_prefix = "upay MTB Islamic DPS - "
sections = []
for block in re.split(r"\n(?=## )", "\n" + body):
    block = block.strip()
    if not block:
        continue
    heading, _, content = block.partition("\n")
    heading = heading.lstrip("# ").strip()
    sections.append({"title": overview_prefix + heading, "text": f"{overview_prefix}{heading}\n{content.strip()}"})
sections.append({
    "title": "Fallback & Uncovered Query Handling",
    "text": ("Fallback & Uncovered Query Handling (AI Agent Directive)\n"
             "If a customer asks something that is not covered by this knowledge base (for example the exact profit rate, "
             "other products or banks, or a problem with their own account), do not guess. Say you do not have that "
             "information, that you are noting the question, and that a customer service person will contact them soon. "
             "Then use the note_customer_query tool. Do not give any phone number."),
})

total = 0
for sec in sections:
    attempts = 0
    while True:
        try:
            ids = add_document(sec["text"], {"title": sec["title"]})
            total += len(ids)
            print(f"  [{len(ids)} chunks] {sec['title']}")
            time.sleep(3)
            break
        except Exception as e:
            attempts += 1
            if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) and attempts < 10:
                wait = 15 * attempts
                print(f"  [Rate limit] waiting {wait}s...")
                time.sleep(wait)
            else:
                raise

print("\n" + "=" * 60)
print("Step 3: Verification")
print("=" * 60)
print(f"Chunks in ChromaDB: {len(list_documents())} (expected {total})")
for q in ("what is the profit rate", "how much is the minimum monthly deposit", "can I withdraw before maturity"):
    print(f"\nQuery: {q}\n" + search_documents(q, k=2)[:400])
print("\nDone.")
