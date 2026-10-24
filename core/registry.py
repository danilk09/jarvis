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

pc_only=True: the action works on the PC itself (apps, music, the screen). On the headless brain
(config.HEADLESS, the Pi) it's run on the PC client instead (core/pc.py), or queued while the
PC is off.
"""


ACTIONS: dict = {}     # type → handler(action, chain) -> str
PC_ONLY: set  = set()  # types that need the PC
_SCHEMAS: list = []    # (type, example action object), in registration order
_RULES:   list = []    # (type, routing rule), in registration order


def action(name, schema=None, rules=(), pc_only=False):
    def register(fn):
        ACTIONS[name] = fn
        if pc_only:
            PC_ONLY.add(name)
        if schema:
            _SCHEMAS.extend((name, s) for s in ([schema] if isinstance(schema, str) else schema))
        _RULES.extend((name, r) for r in rules)
        return fn
    return register


def schemas():
    return [s for _, s in _SCHEMAS]


def rules():
    return [r for _, r in _RULES]
