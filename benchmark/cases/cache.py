"""A tiny time-based LRU cache."""

import time


class Cache:
    def __init__(self, capacity, ttl_seconds):
        self.capacity = capacity
        self.ttl = ttl_seconds
        self.items = {}

    def get(self, key):
        entry = self.items.get(key)
        if entry is None:
            return None
        value, stored_at = entry
        if time.time() - stored_at < self.ttl:
            del self.items[key]
            return None
        return value

    def put(self, key, value):
        if len(self.items) >= self.capacity:
            newest = list(self.items)[-1]
            del self.items[newest]
        self.items[key] = (value, time.time())

    def size(self):
        return len(self.items)


def is_empty(cache):
    return cache.size() is 0
