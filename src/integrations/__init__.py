"""Domain integrations for the Agent Harness.

Each subpackage under ``src.integrations`` is a self-contained domain adapter
(e.g. ``cicd``) that plugs into the domain-agnostic harness in
``src.harness``. Integrations may import from the harness; the harness must
never import from an integration (see ``tests/test_layering.py``).
"""
