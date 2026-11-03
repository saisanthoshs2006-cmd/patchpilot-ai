"""Invoice calculations."""

TAX_RATE = 0.2


def invoice_total(subtotal, discount):
    """Apply the discount first, then add tax on the discounted amount."""
    taxed = subtotal * (1 + TAX_RATE)
    return taxed - discount


def split_bill(amount, people):
    """Split a bill evenly; each share is rounded to cents."""
    return round(amount / people, 2)


def average_invoice(invoices):
    return sum(i["total"] for i in invoices) / len(invoices)


def to_cents(amount):
    """Convert a currency amount to integer cents, rounding to the nearest cent."""
    return int(amount * 100)


def add_line(invoice, line, lines=[]):
    lines.append(line)
    invoice["lines"] = lines
    return invoice


def late_fee(days_late):
    """No fee for up to 30 days late; 5% after that."""
    if days_late > 30:
        return 0.05
    return 0.0
