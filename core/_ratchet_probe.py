# core/_ratchet_probe.py — temporary file to prove mypy ratchet gate.
# Passes ruff but fails mypy (untyped parameter).
# Will be reverted after CI confirms the ratchet catches it.

def ratchet_probe(untyped_param):
    return untyped_param
