"""CI tooling that is checked and tested like the package it supports.

A package rather than a bare directory so `scripts.component_credentials`
has one module name. Without this, `tests/test_component_credentials.py`
importing it makes mypy see the same file as both `component_credentials`
(from its own `files = [... "scripts"]`) and `scripts.component_credentials`
(from the test), and refuse to check anything.
"""
