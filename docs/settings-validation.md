# Startup numeric settings

`HYBRID_SEMANTIC_WEIGHT` must be finite and in [0, 1], default 0.60. Both
endpoints remain accepted. Invalid values fail Settings initialization instead
of failing during search. `SEARCH_CACHE_TTL` must be a positive integer, default
300 seconds. Zero/negative values fail startup instead of creating failed Redis
expiration writes. Constraints apply to environment/direct Settings construction.

Existing Git/body/thread constraints remain unchanged. Defaults, active weights,
generation tokens and cache protocol are unchanged; this is validation, not
retrieval tuning. Restart to load operator changes; .env/services were not changed.

Formatted Pydantic validation errors omit input values while retaining field
names/reasons. Programmatic `.errors()`/JSON structures are not redacted; callers
must avoid logging raw configuration inputs. No general secret scrubber was added.

227 offline tests pass, with isolated no-.env/service tests of defaults, endpoints,
positive TTL, invalid/non-finite values, environment loading and formatted error
redaction. Normal local settings load confirmed 0.60/300 without printing secrets.
No live data/cache/settings/process changes; quality/performance checks inherited.
Different valid weights require ordinary quality evaluation before deployment.
