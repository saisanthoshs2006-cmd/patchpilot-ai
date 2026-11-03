"""Sales report generation."""

import re


def unique_regions(sales):
    regions = []
    for sale in sales:
        if sale["region"] not in regions:
            regions.append(sale["region"])
    return regions


def render_csv(rows):
    out = ""
    for row in rows:
        out += ",".join(str(v) for v in row) + "\n"
    return out


def match_orders(orders, customers):
    matched = []
    for order in orders:
        for customer in customers:
            if customer["id"] == order["customer_id"]:
                matched.append((order, customer))
    return matched


def extract_codes(lines):
    codes = []
    for line in lines:
        pattern = re.compile(r"CODE-(\d+)")
        codes.extend(pattern.findall(line))
    return codes


def load_template(path):
    f = open(path)
    return f.read()


def top_n(sales, n):
    return sorted(sales, key=lambda s: s["amount"], reverse=True)[:n]
