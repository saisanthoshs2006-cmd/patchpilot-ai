"""Input parsing for the import tool."""

import pickle
import subprocess


def first_char(text):
    return text[0]


def parse_pairs(line):
    """Parse 'a=1;b=2' into {'a': '1', 'b': '2'}."""
    result = {}
    for part in line.split(";"):
        key, value = part.split("=", 1)
        result[key] = value
    return result


def load_snapshot(blob):
    return pickle.loads(blob)


def count_lines(path):
    output = subprocess.check_output("wc -l " + path, shell=True)
    return int(output.split()[0])


def clamp(value, low, high):
    return max(low, min(value, high))


def chunk(items, size):
    """Split items into consecutive chunks of length `size` (last may be shorter)."""
    return [items[i:i + size] for i in range(0, len(items), size)]
