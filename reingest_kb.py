"""
Re-ingest RAG Knowledge Base — Clears existing data and re-uploads with proper chunking.

Run this script INSIDE the Docker container or locally with the venv active:
    python reingest_kb.py
"""
import sys
sys.path.append('.')

from agent.rag import _vectorstore, add_document, list_documents

# Step 1: Clear all existing documents
print("=" * 60)
print("Step 1: Clearing existing ChromaDB documents...")
print("=" * 60)

existing = list_documents()
print(f"Found {len(existing)} existing documents/chunks.")

if existing:
    for doc in existing:
        try:
            _vectorstore.delete(ids=[doc["id"]])
        except Exception as e:
            print(f"  Warning: Could not delete {doc['id']}: {e}")
    print(f"Deleted {len(existing)} documents.")
else:
    print("No existing documents to delete.")

# Verify deletion
remaining = list_documents()
print(f"Remaining docs after cleanup: {len(remaining)}")

# Step 2: Re-upload the knowledge base as separate sections
print("\n" + "=" * 60)
print("Step 2: Re-ingesting knowledge base with proper chunking...")
print("=" * 60)

# Each section is uploaded separately with clear metadata
sections = [
    {
        "title": "Babuland Overview & Contact",
        "text": """Babuland Overview & Contact
Babuland is an indoor playground chain in Bangladesh for children. It describes itself as the country's largest and safest indoor playground, with branches in Dhaka, Narayanganj, Cumilla and Chattogram.
Head Office: Awal Center (Level 5), 34 Kemal Ataturk Avenue, Banani, Dhaka 1213, Bangladesh.
Phone: +880 9666 723423
Mobile app: Babuland has an app on Google Play and the Apple App Store for online ticket and membership booking.
Social media: Babuland is on Facebook, Instagram and YouTube."""
    },
    {
        "title": "Babuland Branches - Dhaka",
        "text": """Babuland Branches in Dhaka
Mirpur 2: Level 6, Mirpur Shopping Center Complex, Mirpur Road.
Mirpur 1: Sumi Tower (Level 13), beside Bangladesh Eye Hospital.
Mirpur 12: 16/8 Begum Rokeya Avenue, Ramzan Nessa Super Market.
Shewrapara: 962/A East Shewrapara, Rokeya Sarani, Mirpur.
Uttara - Azampur: Level 2, Atik Tower, 7 Rabindra Sarani.
Uttara - House Building: 12th Floor, Sonargaon Janapath Road.
Wari: Level 2, Rose Valley Shopping Mall, 29 Rankin Street.
Badda: Ground Floor, BTI Premier Plaza, Bir Uttam Rafiqul Islam Sarani.
Dhanmondi: Level 6, Shimanto Shambhar Shopping Mall (near Pilkhana).
Banasree: Block E, Rampura.
Green Road: 3A Road No 4, Dhanmondi area.
Mohammadpur: 24, 1 Taj Mahal Road.
Adabor: Adabor Main Road, near Shyamoli.
Bailey Road: a special birthday party venue."""
    },
    {
        "title": "Babuland Branches - Outside Dhaka",
        "text": """Babuland Branches Outside Dhaka
Narayanganj: Level 9, Almas Point, 2 No Rail Gate.
Cumilla: Level 5, QR Tower.
Chattogram - Halishahar: described as the largest Babuland in Chattogram.
Chattogram - Bahaddarhat: Elegy Sky Park Shopping Mall (Level 7).
Chattogram - Jamal Khan: Level 6, Marsa Center.
Chattogram - Meridian Kohinoor City: Level 9."""
    },
    {
        "title": "Opening Hours",
        "text": """Babuland Opening Hours
Babuland is open 7 days a week.
Friday, Saturday and government holidays: 11:00 AM to 10:00 PM.
Sunday to Thursday: 3:00 PM to 10:00 PM.
The Mirpur 2 branch is closed on Sundays. Other branches are open on Sundays.
Hours can change around holidays, so visitors should call +880 9666 723423 or check with the branch before visiting on a holiday."""
    },
    {
        "title": "Tickets & Pricing",
        "text": """Babuland Tickets & Pricing (2026, all prices plus VAT)
Child Adventure Pass: 400 Taka. Full access to the major rides and play zones.
Guardian Pass: 152 Taka. Entry for parents or guardians, with access to the premium lounge.
Infant Joy Pass: 152 Taka. A safe play zone for babies aged 6 to 11 months.
Babuland Grip Socks: 60 Taka. Anti-slip socks are mandatory for entry.
Tickets can be bought at the branch or through the Babuland app. Prices and offers can differ by branch and over time, so callers should confirm with their branch."""
    },
    {
        "title": "Rides & Play Zones",
        "text": """Babuland Rides & Play Zones
Bumper Car Dash: bumper cars for older kids.
Giant Rainbow Slide.
Ocean Ball Pit.
Super Jump Trampoline.
Soft Play Areas: padded play spaces for infants and toddlers.
Exact rides can vary by branch."""
    },
    {
        "title": "Babuland Club Membership",
        "text": """Babuland Club Membership
The Babuland Club gives members priority entry and special discounts.
Membership can be managed through the Babuland app. No membership fee is listed in this knowledge base, so for the current fee and benefits callers should call +880 9666 723423."""
    },
    {
        "title": "Birthday Parties",
        "text": """Birthday Parties at Babuland
Babuland offers customized birthday party packages and theme decorations.
Bailey Road is a special party venue.
No party package prices are listed in this knowledge base. For package details, prices and availability, callers should call +880 9666 723423 or leave their details so the Babuland team can contact them."""
    },
    {
        "title": "Visitor Guidelines",
        "text": """Visitor Guidelines
Anti-slip grip socks (60 Taka) are mandatory for entry.
Parents and guardians need a Guardian Pass to enter with their child.
The Infant Joy Pass covers the safe zone for babies aged 6 to 11 months.
Opening hours can change on public holidays, so check before visiting."""
    },
    {
        "title": "Fallback & Uncovered Query Handling",
        "text": """Fallback & Uncovered Query Handling (AI Agent Directive)
When a customer asks a question that is not covered by this knowledge base, the AI Agent must not guess. Tell the customer to call Babuland on +880 9666 723423, or offer to take their details so the Babuland team can call them back."""
    },
]

import time

total_chunks = 0
for section in sections:
    success = False
    attempts = 0
    while not success and attempts < 10:
        try:
            doc_ids = add_document(section["text"], {"title": section["title"]})
            chunk_count = len(doc_ids)
            total_chunks += chunk_count
            print(f"  [{chunk_count} chunks] {section['title']}")
            success = True
            time.sleep(5)  # Wait 5 seconds between successful requests
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                attempts += 1
                wait_time = 15 * attempts
                print(f"  [Rate Limit Hit] Waiting {wait_time} seconds before retrying...")
                time.sleep(wait_time)
            else:
                raise e

# Step 3: Verify
print("\n" + "=" * 60)
print("Step 3: Verification")
print("=" * 60)
final_docs = list_documents()
print(f"Total chunks in ChromaDB: {len(final_docs)}")
print(f"Expected chunks from ingestion: {total_chunks}")

# Quick test
print("\n" + "=" * 60)
print("Step 4: Quick search test — 'ticket price'")
print("=" * 60)
from agent.rag import search_documents
result = search_documents("ticket price", k=3)
print(result[:500])
print("\nDone! Re-ingestion complete.")
