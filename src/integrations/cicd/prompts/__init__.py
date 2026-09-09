"""Versioned prompt templates for the CI/CD integration's agents.

Each agent's prompt lives in its own `prompts/*.md` file, not as a Python string
constant -- PLAN.md is explicit about rejecting that alternative ("invisible in diffs
and impossible to attribute an eval regression to"). Every file here starts with a
`version:` front-matter line, a `---` separator, and then the `string.Template` body
that `src.integrations.cicd.rendering.load_prompt_template` parses and
`render_investigator_prompt` / `render_diagnostician_prompt` render over the assembled
failure bundle.

This package intentionally holds no Python beyond this docstring: the templates are
data, loaded by `rendering.py`, not importable modules.
"""
