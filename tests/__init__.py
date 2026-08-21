"""Makes `tests` importable as a package.

`tests/infrastructure/test_traced_ports.py` reuses the fakes in
`tests/application/test_worker.py` deliberately: the property it checks is
that wrapping the ports changes nothing, and the most convincing way to
show that is to run the same fakes and make the same assertions rather than
to maintain a second, subtly different set that could drift into agreeing
with a bug.

Every sibling directory already has an `__init__.py`; this is the one that
was missing.
"""
