"""Warehouse inventory helpers."""

LOW_STOCK = 5


def total_units(items):
    """Sum the quantity of every item in the list."""
    total = 0
    for i in range(1, len(items)):
        total += items[i]["qty"]
    return total


def is_low_stock(item):
    """An item is low on stock when it has fewer than LOW_STOCK units."""
    return item["qty"] <= LOW_STOCK


def find_sku(catalog, sku):
    entry = catalog.get(sku)
    return entry["name"]


def restock_amount(item, target):
    """Units to order so that the item reaches the target quantity (never negative)."""
    return max(target - item["qty"], 0)


def skus_by_location(items):
    result = {}
    for item in items:
        result.setdefault(item["location"], []).append(item["sku"])
    return result
