"""The shop's existing test suite. It passes on the buggy code: the bugs are in untested paths.

PatchPilot reruns this after every patch as the regression check.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import orders  # noqa: E402


def test_order_total_applies_tax():
    lines = [{"price": 10.0, "qty": 2}, {"price": 5.0, "qty": 1}]
    assert orders.order_total(lines, 0.1) == 27.5


def test_apply_discount():
    assert orders.apply_discount(200, 25) == 150


def test_build_receipt():
    order = {"lines": [{"name": "Pen", "price": 1.5}]}
    assert orders.build_receipt(order) == "Pen: 1.50\n"


def test_unique_customers_preserves_order():
    data = [{"customer": "b"}, {"customer": "a"}, {"customer": "b"}]
    assert orders.unique_customers(data) == ["b", "a"]


def test_average_order_value():
    assert orders.average_order_value([{"amount": 10}, {"amount": 20}]) == 15
