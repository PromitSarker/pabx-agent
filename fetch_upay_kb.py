"""Rebuild knowledge/upay_dps.md from the live upay page (stdlib only).

    python3 fetch_upay_kb.py

The page is a Next.js app: the full service text is embedded in the HTML as JSON (__NEXT_DATA__),
so no JavaScript rendering is needed. Output is the ONLY knowledge source for the agent.
"""
import html
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

URL = "https://www.upaybd.com/services/details/mtb-islamic-dps"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge", "upay_dps.md")

BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "table"}


class Flatten(HTMLParser):
    """HTML -> plain text. Tables become rows of 'cell | cell'."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.row, self.cell, self.in_cell = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_cell, self.cell = True, []
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr":
            if any(self.row):
                self.out.append("\n[ROW] " + " | ".join(self.row) + "\n")
            self.row = []
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        (self.cell if self.in_cell else self.out).append(data)


def to_text(fragment: str) -> str:
    p = Flatten()
    p.feed(fragment)
    p.close()
    text = "".join(p.out).replace("\xa0", " ")
    lines = [" ".join(l.split()) for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


def group_matrix(lines):
    """Collapse the '[ROW] 24 | 200' product matrix (it appears twice on the page) into one tidy block."""
    out, plans, i = [], {}, 0
    for l in lines:
        m = re.fullmatch(r"\[ROW\] (\d+) \| ([\d,]+)", l)
        if m:
            plans.setdefault(int(m.group(1)), [])
            if m.group(2) not in plans[int(m.group(1))]:
                plans[int(m.group(1))].append(m.group(2))
            continue
        if re.fullmatch(r"\[ROW\] TENURE IN MONTHS \| Amount", l):
            continue
        out.append(l)
    return out, plans


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    data = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S).group(1))
    page = data["props"]["pageProps"]["page"]["data"]
    row = page["rows"]["data"]
    row = row[0] if isinstance(row, list) else row
    desc = to_text(row["desc"])
    if "Product Matrix" not in desc or "Frequently Asked Questions" not in desc:
        sys.exit("page layout changed: expected 'Product Matrix' and 'Frequently Asked Questions' - not overwriting")

    lines, plans = group_matrix(desc.split("\n"))
    text = "\n".join(lines)
    intro, rest = text.split("Product Matrix:", 1)
    faq = rest.split("Frequently Asked Questions (FAQ):", 1)[1]
    faq = re.sub(r"^\[ROW\] ", "- ", faq, flags=re.M)

    # FAQ: question lines are the ones ending with '?'; the answer is the lines that follow.
    items, cur = [], None
    for l in faq.split("\n"):
        l = re.sub(r"^\d+\.\s*", "", l)
        if l.endswith("?"):
            cur = [l, []]
            items.append(cur)
        elif cur:
            cur[1].append(l)

    md = ["# MTB Islamic DPS on upay (source: " + URL + ")", "",
          "## Overview", intro.strip(), "",
          "## Plans available (every tenure can be combined with every monthly amount)"]
    for months in sorted(plans):
        md.append(f"- {months} months: monthly deposit of {', '.join(plans[months])} Taka")
    md.append("")
    for q, a in items:
        body = "\n".join(a).strip()
        if re.search(r"given in the below table", body):  # the table itself is rendered above as 'Plans available'
            body = "See the plans listed above: tenures of 24, 36 or 48 months, each with a monthly deposit of 200, 500, 1,000 or 2,000 Taka."
        md += [f"## {q}", body, ""]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write("\n".join(md).strip() + "\n")
    print(f"wrote {OUT}: {len(items)} FAQ entries, {len(plans)} tenures")


if __name__ == "__main__":
    main()
