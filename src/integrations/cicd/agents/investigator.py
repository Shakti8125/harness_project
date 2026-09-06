"""Investigator agent: deterministic collection (jobs, failed jobs, logs, baseline,
compare, manifest parse) plus exactly one LLM call producing InvestigationNotes.

Not a ReAct loop — the LLM call proposes at most 3 additional read-only tool calls,
non-read requests are dropped and recorded. Built in Phase 1.
"""
