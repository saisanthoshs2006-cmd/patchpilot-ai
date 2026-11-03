"""User profile helpers."""

import re


def parse_username(email):
    m = re.match(r"([^@]+)@", email)
    return m.group(1)


def display_name(user):
    return user["first"] + " " + user["last"]


def parse_age(value):
    try:
        return int(value)
    except:
        pass


def is_adult(age):
    if age >= 18:
        return True
    else:
        return False
    print("checked age", age)


def initials(name):
    """Initials of each word, e.g. 'Ada Lovelace' -> 'AL'."""
    return "".join(part[0].upper() for part in name.split())
