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

## Deep syntax trees

Symbol extraction uses an explicit traversal stack rather than recursive Python
calls. This avoids `RecursionError` for deeply nested Tree-sitter expression
trees, including a valid 1,500-term binary expression followed by a function.
Traversal still visits children in the original preorder and carries the same
class/function scope, preserving existing names, classifications, code and line
locations. This does not impose new source-size limits or guarantee bounded
memory for arbitrary input.

On 2026-10-02, old/new extraction was compared read-only across all 219 local
Python files: every symbol field and ordering matched. The deep-expression
fixture failed before the change and passes after it; nested class, method,
local-function and sibling ordering have explicit regression coverage. Live
retrieval evaluation retained all baseline metrics on the existing index; the
corpus was not rebuilt.
