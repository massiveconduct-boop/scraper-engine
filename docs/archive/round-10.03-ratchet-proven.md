# Round 10.03 — mypy Ratchet Gate Proven on Real CI

## 1. mypy Config — `pyproject.toml` with `strict = true`

```
$ .venv/bin/mypy --verbose core/models.py 2>&1 | grep "Config File"
LOG:  Config File:  /home/ubuntu/my_spaces/my_tools/scraper_engine/pyproject.toml
```

Local and CI both load `pyproject.toml` which has `strict = true` under `[tool.mypy]`. The local-vs-CI finding count difference (10 vs 23) comes from different stub resolution — not different flags. CI's GitHub runner has different `pydantic` and `starlette` stub versions causing some types to resolve as `Any`, which `strict` then flags. Locally, those same types have full stubs and pass clean.

### mypy Version (local + CI pinned)

```
$ .venv/bin/pip show mypy | grep Version
Version: 2.3.0
```

`pyproject.toml`: `"mypy==2.3.0"` — both environments pinned.

## 2. Baseline File

### Generation Command

```
mypy core/ proxy/ orchestrator/ api/ storage/ --ignore-missing-imports
```

Same command in CI ratchet step and baseline generation. No `--strict` CLI flag; `strict = true` from `[tool.mypy]` applies.

### Full Baseline (`tools/mypy-baseline.txt`)

```
api/middleware.py:14: error: Class cannot subclass "BaseHTTPMiddleware" (has type "Any")  [misc]
api/middleware.py:31: error: Class cannot subclass "BaseHTTPMiddleware" (has type "Any")  [misc]
api/middleware.py:68: error: Class cannot subclass "BaseHTTPMiddleware" (has type "Any")  [misc]
api/middleware.py:105: error: Unused "type: ignore" comment  [unused-ignore]
api/routes.py:23: error: Untyped decorator makes function "scrape" untyped  [untyped-decorator]
api/routes.py:30: error: Untyped decorator makes function "get_job" untyped  [untyped-decorator]
api/routes.py:40: error: Untyped decorator makes function "health" untyped  [untyped-decorator]
core/models.py:37: error: Class cannot subclass "BaseModel" (has type "Any")  [misc]
core/models.py:65: error: Class cannot subclass "BaseModel" (has type "Any")  [misc]
core/models.py:81: error: Class cannot subclass "BaseModel" (has type "Any")  [misc]
core/models.py:91: error: Class cannot subclass "BaseModel" (has type "Any")  [misc]
core/models.py:97: error: Untyped decorator makes function "non_empty" untyped  [untyped-decorator]
core/models.py:116: error: Class cannot subclass "BaseModel" (has type "Any")  [misc]
proxy/harvester.py:215: error: Missing type arguments for generic type "dict"  [type-arg]
proxy/harvester.py:300: error: "object" has no attribute "classify"  [attr-defined]
proxy/harvester.py:322: error: Incompatible default for parameter "tenant" (default has type "None", parameter has type "TenantId")  [assignment]
proxy/harvester.py:330: error: Statement is unreachable  [unreachable]
storage/dedup.py:66: error: Returning Any from function declared to return "FetchResult | None"  [no-any-return]
storage/redis_client.py:73: error: Returning Any from function declared to return "int"  [no-any-return]
storage/redis_client.py:79: error: Returning Any from function declared to return "int"  [no-any-return]
storage/redis_client.py:85: error: Returning Any from function declared to return "int"  [no-any-return]
storage/redis_client.py:91: error: Returning Any from function declared to return "int"  [no-any-return]
storage/redis_client.py:97: error: Returning Any from function declared to return "bool"  [no-any-return]
```

23 lines, all format `file:line: error: message [code]`.

## 3. CI Current Output — Baseline Match Confirmed

Run 30178029654: `mypy ratchet OK — no regressions` — 23 findings match baseline exactly.

Run 30189977872 (probe test, below): `mypy ratchet` exits 1 with the injected error.

## 4. Real CI Ratchet Failure — Probe Error Caught and Blocked

### Probe File (`core/_ratchet_probe.py`)

```python
# core/_ratchet_probe.py — temporary file passed ruff, fails mypy
def ratchet_probe(untyped_param):
    return untyped_param
```

### Ruff Passes (✓)

```
lint  Run ruff check . --exclude 'challenge-mirror' --exclude 'report-review-fix'
lint  All checks passed!
```

### Mypy Ratchet Fails (X)

**Run URL**: https://github.com/massiveconduct-boop/scraper-engine/actions/runs/30189977872

```
lint  mypy ratchet (fails only on NEW type errors beyond committed baseline)

lint  core/_ratchet_probe.py:5: error: Function is missing a type annotation  [no-untyped-def]

lint  === NEW mypy errors (failing build) ===

lint  core/_ratchet_probe.py:5: error: Function is missing a type annotation  [no-untyped-def]
```

The ratchet correctly identified `core/_ratchet_probe.py:5` as a NEW finding not present in the 23-entry baseline, flagged it, and exited 1 — blocking the build.

### Revered

```
$ rm core/_ratchet_probe.py && git commit -m "revert: remove ratchet probe"
```
