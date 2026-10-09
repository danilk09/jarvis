"""
Action registry. Each action type registers its handler together with the
lines that teach Claude how to call it, so adding a command is one function:

    @action("weather",
            schema='{"type":"weather","city":"<city>"}',
            rules=['"weather in X" → type "weather"'])
    def _weather(action, chain):
        ...
        return "short result for the log"

Handlers receive the action dict from Claude and a `chain` dict shared by all
actions in one response (e.g. a screenshot's analysis feeding generate_file).
"""

ACTIONS: dict = {}     # type → handler(action, chain) -> str
_SCHEMAS: list = []    # example action objects, in registration order
_RULES:   list = []    # routing rules, in registration order


def action(name, schema=None, rules=()):
    def register(fn):
        ACTIONS[name] = fn
        if schema:
            _SCHEMAS.extend([schema] if isinstance(schema, str) else schema)
        _RULES.extend(rules)
        return fn
    return register


def schemas():
    return list(_SCHEMAS)


def rules():
    return list(_RULES)
