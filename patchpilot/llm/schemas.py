"""JSON schemas the model must answer in (enforced server-side via structured outputs).

Numeric ranges aren't expressed in the schema; the engine validates them after parsing.
"""

from __future__ import annotations

AI_CATEGORIES = ["bug", "runtime_error", "security", "performance", "code_smell", "maintainability", "best_practice"]
AI_SEVERITIES = ["critical", "high", "medium", "low"]

PATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "replacement_code": {
            "type": "string",
            "description": "The complete fixed version of the given code block, same indentation, nothing else.",
        },
        "new_imports": {
            "type": "array", "items": {"type": "string"},
            "description": "Import statements the fix needs that the module doesn't already have, e.g. 'import json'.",
        },
        "rationale": {"type": "string", "description": "One or two sentences: what changed and why it fixes the issue."},
        "test_code": {
            "type": "string",
            "description": "A self-contained pytest file that FAILS on the current code and PASSES on the fixed code. "
                           "Empty string if no meaningful behavioral test is possible.",
        },
    },
    "required": ["replacement_code", "new_imports", "rationale", "test_code"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "function": {"type": "string", "description": "Name of the function containing the issue."},
                    "line": {"type": "integer", "description": "Line number exactly as numbered in the input."},
                    "quoted_code": {
                        "type": "string",
                        "description": "The code on that line, copied verbatim (without the line-number prefix).",
                    },
                    "category": {"type": "string", "enum": AI_CATEGORIES},
                    "severity": {"type": "string", "enum": AI_SEVERITIES},
                    "title": {"type": "string", "description": "One line, under 80 characters."},
                    "explanation": {"type": "string", "description": "What is wrong, concretely."},
                    "why_it_matters": {"type": "string", "description": "The concrete consequence for users or the business."},
                    "suggested_fix": {"type": "string", "description": "The specific change to make."},
                    "failing_input": {
                        "type": "string",
                        "description": "A concrete Python call that triggers the bug, e.g. paginate(list(range(25)), 1).",
                    },
                    "expected_behavior": {"type": "string"},
                    "actual_behavior": {"type": "string"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0: how sure you are this is a real defect."},
                },
                "required": [
                    "function", "line", "quoted_code", "category", "severity", "title", "explanation",
                    "why_it_matters", "suggested_fix", "failing_input", "expected_behavior", "actual_behavior",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}
