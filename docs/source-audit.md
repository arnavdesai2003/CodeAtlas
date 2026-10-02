# Python source audit

Run `.venv/bin/python -B -m scripts.audit_python_sources` to inspect registered
Python files, optionally restricting to `--repository-id ID`. The command reads
PostgreSQL metadata and local files only; it does not synchronize Git, replace
symbols, publish Elasticsearch documents or invalidate Redis.

JSON reports repository/file counts and findings with repository ID, file ID,
relative path and category. Exit status is 1 when findings exist, 0 otherwise.
Missing repository IDs and unsafe clone boundaries abort with an error. Findings
are `unavailable_or_excluded` (missing or rejected source path),
`source_read_error` (including encoding failures), or `grammar_recovery`
(Tree-sitter's root reports an error or missing node). Python encoding cookies
and source path exclusions match indexing. No source text is printed.

This checks the installed Tree-sitter grammar, not Python compilation or runtime
semantics. A clean result is not proof that extracted symbols are complete or
correct. Metadata and files can change during inspection; this is not a locked
snapshot. Indexing continues to accept Tree-sitter recovery trees; this audit
introduces no rejection policy.

On 2026-10-02, a read-only local audit found 219 registered Python files in six
repositories, all clean. No corpus, index, cache or running API was changed.
Offline tests cover valid encoded source, grammar recovery, invalid encoding,
missing/excluded paths, repository selection and unchanged metadata.
