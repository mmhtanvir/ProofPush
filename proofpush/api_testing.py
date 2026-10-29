"""
API Payload Testing.
Discovers endpoints (OpenAPI first, then framework routes and the API calls the frontend already makes),
builds valid and invalid requests, sends the same deterministic requests to the baseline and the candidate,
and classifies each difference using, in order: explicit assertions, the documented contract, then baseline
behaviour. A difference from the baseline alone is only a *potential* regression.

Safety: production runs only approved read-only cases; writes need `allow_writes` (local/staging) and are
refused when the app points at a remote database; request count, rate, timeout and body size are capped.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from proofpush import assertions
from proofpush.classifier import Finding
from proofpush.utils import redact_secrets, urlopen

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|api[-_]?key|authori[sz]ation|cookie|session|credit|card|cvv|ssn|otp", re.I)
# Write endpoints whose real side effects ProofPush never triggers on its own, even with allow_writes
SIDE_EFFECT_PATH = re.compile(r"pay|charge|refund|checkout|invoice|email|mail|sms|notify|send|webhook|upload|import|export|deploy|purge", re.I)
DYNAMIC_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$|"
                           r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
REJECTED = {400, 401, 403, 404, 405, 409, 413, 415, 422}
OPENAPI_FILES = ["openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml",
                 "docs/openapi.json", "docs/openapi.yaml", "api/openapi.json", "api/openapi.yaml", "schema.yml", "schema.yaml"]
OPENAPI_URLS = ["/openapi.json", "/api/openapi.json", "/api/schema/?format=json", "/swagger.json", "/v3/api-docs"]


# --- model ---------------------------------------------------------------------------------------------------

@dataclass
class Endpoint:
    method: str
    path: str  # with {param} placeholders
    source: str  # openapi | framework:<name> | frontend-traffic | config
    confidence: float
    params: List[Dict[str, Any]] = field(default_factory=list)  # OpenAPI parameter objects
    body_schema: Optional[Dict[str, Any]] = None
    body_kind: str = "json"  # json | form
    responses: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)  # "200" -> schema
    requires_auth: Optional[bool] = None
    source_file: Optional[str] = None
    sample_path: Optional[str] = None  # a concrete URL seen in real traffic (known-good values)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class ApiCase:
    name: str
    method: str
    path: str  # concrete
    endpoint: Optional[Endpoint] = None
    query: Dict[str, Any] = field(default_factory=dict)
    body: Any = None
    body_kind: str = "json"  # json | form | raw
    headers: Dict[str, str] = field(default_factory=dict)
    with_auth: bool = True
    kind: str = "valid"  # valid | invalid | unauth | other_user | permission | explicit
    allowed: Optional[bool] = None  # permission cases: whether this identity is supposed to succeed
    identity: str = ""  # which configured test identity sends it ("" = the main one)
    expect_status: Optional[List[int]] = None
    expect_body: Dict[str, Any] = field(default_factory=dict)  # "$.a.b[0]" -> value
    expect_fields: List[str] = field(default_factory=list)
    max_ms: Optional[int] = None
    prod: bool = False  # approved for production
    guard: bool = False  # pins a previously fixed bug: any failure blocks, even if the baseline fails too
    save: Dict[str, str] = field(default_factory=dict)  # name -> "$.id", for later steps of the same case
    assertions: List[str] = field(default_factory=list)  # expressions over response/request/vars (assertions.py)
    config_errors: List[str] = field(default_factory=list)  # invalid assertions: the case is not sent
    steps: List["ApiCase"] = field(default_factory=list)

    @property
    def writes(self) -> bool:
        return self.method.upper() not in SAFE_METHODS or any(s.writes for s in self.steps)


@dataclass
class ApiResult:
    status: Optional[int] = None
    ms: int = 0
    body: Any = None
    error: Optional[str] = None  # connection refused, timeout...
    step_failures: List[str] = field(default_factory=list)
    vars: Dict[str, str] = field(default_factory=dict)  # values saved by the case's steps


# --- discovery -----------------------------------------------------------------------------------------------

def _resolve(node: Any, spec: Dict[str, Any], ref_depth: int = 0) -> Any:
    """Inline local $refs (#/components/...). Only $ref hops are limited, so self-referencing schemas stop."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            if ref_depth >= 6:
                return {}
            target: Any = spec
            for part in node["$ref"][2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(target, spec, ref_depth + 1)
        return {k: _resolve(v, spec, ref_depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, spec, ref_depth) for v in node]
    return node


def parse_openapi(text: str) -> Optional[Dict[str, Any]]:
    try:
        spec = json.loads(text)
    except ValueError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    return spec if isinstance(spec, dict) and ("openapi" in spec or "swagger" in spec) and "paths" in spec else None


def load_openapi(repo_path: Path, configured: str = "", base_url: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """(spec, where it came from). Configured path/URL first, then common files, then the running app's schema URL."""
    candidates = [configured] if configured else []
    for c in candidates + ([] if configured else OPENAPI_FILES):
        if c and not c.startswith(("/", "http")) and (repo_path / c).is_file():
            spec = parse_openapi((repo_path / c).read_text(encoding="utf-8", errors="replace"))
            if spec:
                return spec, c
    urls = [configured] if configured.startswith(("/", "http")) else ([] if configured else OPENAPI_URLS)
    for u in urls:
        if not base_url and not u.startswith("http"):
            continue
        res = http_call(base_url or "", ApiCase(name="openapi", method="GET", path=u), headers or {}, timeout=5)
        if res.status == 200 and isinstance(res.body, dict):
            spec = parse_openapi(json.dumps(res.body))
            if spec:
                return spec, u
    return None, ""


def endpoints_from_openapi(spec: Dict[str, Any]) -> List[Endpoint]:
    spec = _resolve(spec, spec)
    global_security = bool(spec.get("security"))
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", [])
        for method, op in item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} or not isinstance(op, dict):
                continue
            body_schema, body_kind = None, "json"
            content = (op.get("requestBody") or {}).get("content") or {}
            for ctype, kind in (("application/json", "json"), ("application/x-www-form-urlencoded", "form")):
                if ctype in content:
                    body_schema, body_kind = (content[ctype] or {}).get("schema") or {}, kind
                    break
            if not body_schema and "parameters" in op:  # Swagger 2 body parameter
                for p in op["parameters"]:
                    if p.get("in") == "body":
                        body_schema = p.get("schema") or {}
            responses = {}
            for status, resp in (op.get("responses") or {}).items():
                schema = None
                if isinstance(resp, dict):
                    rc = resp.get("content") or {}
                    schema = (rc.get("application/json") or {}).get("schema") if rc else resp.get("schema")
                responses[str(status)] = schema
            security = op.get("security", None)
            out.append(Endpoint(
                method=method.upper(), path=path, source="openapi", confidence=0.95,
                params=[p for p in shared + op.get("parameters", []) if isinstance(p, dict) and p.get("in") in ("path", "query")],
                body_schema=body_schema, body_kind=body_kind, responses=responses,
                requires_auth=bool(security) if security is not None else global_security,
            ))
    return out


_FRAMEWORK_PATTERNS = [
    # (framework, regex with groups (method, path) or (path, methods), file glob)
    ("fastapi/flask", re.compile(r"""@\w+\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']"""), "*.py"),
    ("flask", re.compile(r"""@\w+\.route\(\s*["']([^"']+)["'](?:[^)]*methods\s*=\s*\[([^\]]*)\])?"""), "*.py"),
    ("express", re.compile(r"""\b(?:app|router|server)\.(get|post|put|patch|delete)\(\s*["'`](/[^"'`]*)["'`]"""), "*.[jt]s"),
    ("go", re.compile(r"""\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\(\s*"(/[^"]*)\""""), "*.go"),
    ("go", re.compile(r"""HandleFunc\(\s*"(?:(GET|POST|PUT|PATCH|DELETE) )?(/[^"]*)\""""), "*.go"),
]
_SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", ".next", "dist", "build", "out", "coverage", "__pycache__",
              "tests", "test", "vendor", "migrations"}


def _normalize_path(path: str) -> str:
    """Express :id, Django <int:pk>, Next.js [id] -> {id}."""
    path = re.sub(r":(\w+)", r"{\1}", path)
    path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)
    path = re.sub(r"\[\.{0,3}(\w+)\]", r"{\1}", path)
    return path if path.startswith("/") else "/" + path


def discover_framework_endpoints(root: Path) -> List[Endpoint]:
    found: Dict[str, Endpoint] = {}

    def add(method: str, path: str, framework: str, f: Path):
        ep = Endpoint(method=method.upper(), path=_normalize_path(path), source=f"framework:{framework}", confidence=0.6,
                      source_file=str(f.relative_to(root)))
        found.setdefault(ep.key, ep)

    def files(pattern: str):
        for f in root.rglob(pattern):
            if not any(part in _SKIP_DIRS for part in f.relative_to(root).parts):
                yield f

    for framework, regex, pattern in _FRAMEWORK_PATTERNS:
        for f in files(pattern):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in regex.finditer(text):
                if framework == "flask":
                    methods = re.findall(r"[A-Za-z]+", m.group(2) or "GET")
                    for method in methods:
                        add(method, m.group(1), framework, f)
                else:
                    add(m.group(1) or "GET", m.group(2), framework, f)
    # Django / DRF: urls.py paths (methods unknown -> GET) and router.register prefixes (list + detail)
    for f in files("urls.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"""\b(?:re_)?path\(\s*r?["']\^?([^"'(]*?)\$?["']""", text):
            if "include(" not in text[m.end():m.end() + 80].split(")")[0] and m.group(1):
                add("GET", m.group(1), "django", f)
        for m in re.finditer(r"""\.register\(\s*r?["']\^?([^"']+?)\$?["']""", text):
            prefix = "/" + m.group(1).strip("/")
            add("GET", prefix + "/", "drf", f)
            add("POST", prefix + "/", "drf", f)
            add("GET", prefix + "/{pk}/", "drf", f)
    # Next.js route handlers
    for f in files("route.*"):
        rel = f.relative_to(root).as_posix()
        if "/api/" in "/" + rel and f.suffix in (".js", ".ts"):
            api = "/" + rel.split("app/", 1)[-1].rsplit("/route.", 1)[0]
            for method in re.findall(r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE)\b", f.read_text(errors="ignore")):
                add(method, re.sub(r"/\([^)]*\)", "", api), "nextjs", f)
    for base in ("pages/api", "src/pages/api"):
        for f in (root / base).rglob("*.[jt]s") if (root / base).is_dir() else []:
            route = "/api/" + f.relative_to(root / base).with_suffix("").as_posix()
            add("GET", re.sub(r"/index$", "", route), "nextjs", f)
    return list(found.values())


def endpoints_from_traffic(calls: Dict[str, Dict[str, Any]]) -> List[Endpoint]:
    """GET calls the frontend really made (concrete URLs with known-good values)."""
    out = []
    for key, call in calls.items():
        method, _, path = key.partition(" ")
        if method == "GET" and call.get("sample") and call.get("status", 500) < 400:
            out.append(Endpoint(method="GET", path=path.split("?")[0].replace(":id", "{id}"), source="frontend-traffic",
                                confidence=0.9, sample_path=call["sample"]))
    return out


def merge_endpoints(*groups: List[Endpoint]) -> List[Endpoint]:
    """Higher-priority sources first; later sources only add endpoints (or a traffic sample) not already known."""
    merged: Dict[str, Endpoint] = {}
    for group in groups:
        for ep in group:
            known = merged.get(ep.key)
            if known is None:
                merged[ep.key] = ep
            elif ep.sample_path and not known.sample_path:
                known.sample_path = ep.sample_path
    return list(merged.values())


# --- payload generation --------------------------------------------------------------------------------------

def sample_value(schema: Optional[Dict[str, Any]], name: str = "") -> Any:
    """A deterministic value that satisfies the schema (examples and enums first)."""
    s = schema or {}
    if "example" in s:
        return s["example"]
    if s.get("examples") and isinstance(s["examples"], list):
        return s["examples"][0]
    if s.get("enum"):
        return s["enum"][0]
    if "default" in s:
        return s["default"]
    for combo in ("allOf", "oneOf", "anyOf"):
        if s.get(combo):
            if combo == "allOf":
                merged = {"type": "object", "properties": {}, "required": []}
                for part in s["allOf"]:
                    merged["properties"].update(part.get("properties", {}))
                    merged["required"] += part.get("required", [])
                return sample_value(merged, name)
            return sample_value(s[combo][0], name)
    kind = s.get("type") or ("object" if "properties" in s else "string")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object":
        props = s.get("properties", {})
        return {k: sample_value(v, k) for k, v in props.items() if k in s.get("required", props.keys())}
    if kind == "array":
        return [sample_value(s.get("items"), name) for _ in range(max(1, s.get("minItems", 1)))]
    if kind == "integer":
        low = s.get("minimum", 1)
        return int(low) + (1 if s.get("exclusiveMinimum") is True else 0)
    if kind == "number":
        return float(s.get("minimum", 1))
    if kind == "boolean":
        return True
    fmt = s.get("format", "")
    by_format = {"email": "proofpush-test@example.com", "date-time": "2026-01-01T00:00:00Z", "date": "2026-01-01",
                 "uuid": "00000000-0000-4000-8000-000000000000", "uri": "https://example.com/", "url": "https://example.com/",
                 "ipv4": "192.0.2.1", "password": "Test-passw0rd!"}
    if fmt in by_format:
        return by_format[fmt]
    text = "proofpush-test"
    if s.get("maxLength"):
        text = text[: s["maxLength"]]
    return text + "x" * max(0, s.get("minLength", 0) - len(text))


def invalid_bodies(schema: Optional[Dict[str, Any]], limit: int) -> List[Tuple[str, Any, str]]:
    """(name, body, body_kind) variants that a correct API must reject: missing/null/wrong-type/empty/enum/range, malformed JSON."""
    s = schema or {}
    valid = sample_value(s)
    out: List[Tuple[str, Any, str]] = []
    if isinstance(valid, dict):
        props = s.get("properties", {})
        for f in s.get("required", [])[:3]:
            out.append((f"missing required '{f}'", {k: v for k, v in valid.items() if k != f}, "json"))
            out.append((f"null '{f}'", {**valid, f: None}, "json"))
            p = props.get(f, {})
            t = p.get("type")
            wrong = "not-a-number" if t in ("integer", "number") else (["x"] if t == "string" else "not-an-object")
            out.append((f"wrong type for '{f}'", {**valid, f: wrong}, "json"))
            if t == "string" and (p.get("minLength", 0) > 0 or p.get("format") or p.get("enum")):
                out.append((f"empty '{f}'", {**valid, f: ""}, "json"))
            if p.get("enum"):
                out.append((f"invalid enum for '{f}'", {**valid, f: "proofpush-not-an-option"}, "json"))
            if "maximum" in p:
                out.append((f"'{f}' above maximum", {**valid, f: p["maximum"] + 1}, "json"))
            if "minimum" in p:
                out.append((f"'{f}' below minimum", {**valid, f: p["minimum"] - 1}, "json"))
            if p.get("maxLength"):
                out.append((f"'{f}' too long", {**valid, f: "x" * (p["maxLength"] + 1)}, "json"))
    out.append(("malformed JSON", '{"proofpush": ', "raw"))
    return out[:limit]


def _fill_path(ep: Endpoint) -> Tuple[str, Dict[str, Any]]:
    """Concrete path + required query values: a real traffic sample if we have one, else schema/examples."""
    if ep.sample_path:
        parsed = urllib.parse.urlparse(ep.sample_path)
        return parsed.path, {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    path, query = ep.path, {}
    by_name = {p.get("name"): p for p in ep.params}
    for name in re.findall(r"\{(\w+)\}", ep.path):
        p = by_name.get(name, {})
        value = p.get("example", sample_value(p.get("schema") or {"type": "integer"}, name))
        path = path.replace("{" + name + "}", urllib.parse.quote(str(value)))
    for p in ep.params:
        if p.get("in") == "query" and p.get("required"):
            query[p["name"]] = p.get("example", sample_value(p.get("schema"), p["name"]))
    return path, query


def generate_cases(endpoints: List[Endpoint], max_invalid: int = 4, auth_configured: bool = False,
                   other_identity: Optional[str] = None) -> List[ApiCase]:
    cases: List[ApiCase] = []
    for ep in endpoints:
        path, query = _fill_path(ep)
        documented_ok = [int(s) for s in ep.responses if s.isdigit() and int(s) < 400] or None
        body = sample_value(ep.body_schema) if ep.body_schema else None
        base = dict(method=ep.method, path=path, endpoint=ep, query=query, body_kind=ep.body_kind)
        cases.append(ApiCase(name="valid request", body=body, kind="valid", expect_status=documented_ok, **base))
        if ep.body_schema and ep.method not in SAFE_METHODS:
            documented_err = [int(s) for s in ep.responses if s.isdigit() and int(s) in REJECTED] or None
            for name, bad, kind in invalid_bodies(ep.body_schema, max_invalid):
                cases.append(ApiCase(name=name, kind="invalid", body=bad, expect_status=documented_err,
                                     **{**base, "body_kind": kind}))
        for p in ep.params:
            if p.get("in") == "query" and (p.get("schema") or {}).get("type") in ("integer", "number"):
                cases.append(ApiCase(name=f"wrong type for query '{p['name']}'", kind="invalid", body=body,
                                     **{**base, "query": {**query, p["name"]: "not-a-number"}}))
                break
        if ep.requires_auth or (ep.requires_auth is None and auth_configured and ep.source != "frontend-traffic"):
            cases.append(ApiCase(name="without credentials", kind="unauth", body=body, with_auth=False,
                                 expect_status=[401, 403] if ep.requires_auth else None, **base))
        # Ownership: a specific resource read by a second test user (IDOR) should be denied
        if other_identity and ep.method == "GET" and ("{" in ep.path or ep.sample_path) and ep.requires_auth is not False:
            cases.append(ApiCase(name=f"as another test user ({other_identity})", kind="other_user",
                                 identity=other_identity, **base))
    return cases


SENSITIVE_FIELD = re.compile(r"^(password|password_?hash|passwd|secret|client_secret|api_?key|access_?token|refresh_?token|"
                             r"token|session_?id|ssn|card_?number|cvv|private_?key|otp_?secret|totp_?secret)$", re.I)


def permission_cases(rules: List[Dict[str, Any]], endpoints: List[Endpoint], identities: List[str]) -> Tuple[List[ApiCase], List[str]]:
    """Cases for a permission matrix; returns (cases, problems) where problems are rules that can't be exercised.

    Each rule: {path, <METHOD>: [allowed identities]}. Identities: owner (the main credentials), anonymous,
    and any configured `<env>:<name>`; every identity not listed for a method must be denied.
    """
    cases, problems = [], []
    known = {e.key: e for e in endpoints}
    everyone = ["owner", "anonymous"] + [i for i in identities if i not in ("owner", "anonymous")]
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("path"):
            problems.append(f"permission rule without a path: {rule}")
            continue
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            if method not in rule:
                continue
            allowed = set(rule[method] or [])
            unknown = allowed - set(everyone)
            if unknown:
                problems.append(f"{method} {rule['path']}: no credentials for {', '.join(sorted(unknown))}")
            ep = known.get(f"{method} {rule['path']}") or Endpoint(method=method, path=rule["path"], source="config", confidence=1.0)
            if "{" in rule["path"] and not (ep.params or ep.sample_path) and not rule.get("example_path"):
                problems.append(f"{method} {rule['path']}: add example_path (a concrete URL to a test-owned resource)")
                continue
            path, query = (rule["example_path"], {}) if rule.get("example_path") else _fill_path(ep)
            body = sample_value(ep.body_schema) if ep.body_schema else rule.get("payload")
            for who in everyone:
                ok = who in allowed
                cases.append(ApiCase(
                    name=f"{who} {'may' if ok else 'must not'} {method}", method=method, path=path, endpoint=ep,
                    query=query, body=body, kind="permission", allowed=ok,
                    identity="" if who in ("owner", "anonymous") else who, with_auth=who != "anonymous",
                    expect_status=None if ok else [401, 403, 404],
                ))
    return cases, problems


def cases_from_config(raw_cases: List[Dict[str, Any]]) -> List[ApiCase]:
    """Developer-written cases: the strongest expectations ProofPush has."""
    def one(c: Dict[str, Any]) -> ApiCase:
        expect = c.get("expect") or {}
        status = expect.get("status")
        body, kind = ((c["json"], "json") if "json" in c else (c["payload"], "json") if "payload" in c
                      else (c["form"], "form") if "form" in c else (None, "json"))
        checks = [str(x) for x in c.get("assertions") or []]
        return ApiCase(
            name=str(c.get("name") or f"{c.get('method', 'GET')} {c.get('path', '/')}"), method=str(c.get("method", "GET")).upper(),
            path=str(c.get("path", "/")), query=dict(c.get("query") or {}), body=body, body_kind=kind,
            headers={str(k): str(v) for k, v in (c.get("headers") or {}).items()}, with_auth=bool(c.get("auth", True)),
            kind="explicit", expect_status=[status] if isinstance(status, int) else status,
            expect_body=dict(expect.get("body") or {}), expect_fields=list(expect.get("fields") or []),
            max_ms=expect.get("max_ms"), prod=bool(c.get("prod", False)), guard=bool(c.get("guard", False)),
            identity=str(c.get("as") or ""),
            save=dict(c.get("save") or {}), assertions=checks, config_errors=assertions.validate_all(checks),
        )
    out = []
    for c in raw_cases or []:
        if not isinstance(c, dict):
            continue
        if c.get("steps"):
            head = one({**c["steps"][0], "name": c.get("name"), "prod": c.get("prod", False), "guard": c.get("guard", False)})
            head.steps = [one(s) for s in c["steps"][1:]]
            head.config_errors += [e for s in head.steps for e in s.config_errors]
            out.append(head)
        else:
            out.append(one(c))
    return out


# --- safety policy -------------------------------------------------------------------------------------------

@dataclass
class Policy:
    env_type: str  # local | staging | production
    allow_writes: bool = False
    remote_database: Optional[str] = None  # why the app's database looks shared/remote

    def refusal(self, case: ApiCase, identities: Optional[Dict[str, Dict[str, str]]] = None) -> Optional[str]:
        """Why this case must not be sent, or None. Production is read-only and explicit-only by default."""
        if case.config_errors:
            return "invalid assertion in .proofpush.yml: " + "; ".join(case.config_errors[:2])
        if case.identity and identities is not None and case.identity not in identities:
            return f"test identity '{case.identity}' has no credentials (api_tests.auth.<env>:{case.identity})"
        if self.env_type == "production":
            if case.kind != "explicit" or not case.prod:
                return "production runs only approved cases (prod: true)"
            if case.writes and not self.allow_writes:
                return "write requests are never sent to production unless the environment is a test tenant (allow_writes)"
        if case.writes:
            if not self.allow_writes:
                return "write requests need api_tests.allow_writes"
            if self.remote_database and self.env_type == "local":
                return f"writes refused: the app's database is not isolated ({self.remote_database})"
            if case.kind != "explicit" and SIDE_EFFECT_PATH.search(case.path):
                return "endpoint may trigger real side effects (payments, email, uploads); add an explicit case to test it"
        return None


# --- HTTP ----------------------------------------------------------------------------------------------------

def http_call(base_url: str, case: ApiCase, auth_headers: Dict[str, str], timeout: float = 10,
              max_body: int = 1_000_000, variables: Optional[Dict[str, str]] = None) -> ApiResult:
    variables = variables or {}

    def fill(text: str) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: urllib.parse.quote(str(variables.get(m.group(1), m.group(0)))), text)

    url = base_url.rstrip("/") + fill(case.path) if not case.path.startswith("http") else case.path
    if case.query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: fill(str(v)) for k, v in case.query.items()})
    headers = {"User-Agent": "proofpush-api-tests", "Accept": "application/json", **case.headers}
    if case.with_auth:
        headers.update(auth_headers)
    data = None
    if case.body is not None and case.method not in ("GET", "HEAD"):
        if case.body_kind == "form":
            data = urllib.parse.urlencode(case.body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif case.body_kind == "raw":
            data = str(case.body).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = json.dumps(_fill_obj(case.body, variables)).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=case.method, headers=headers)
    start = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(max_body + 1)
    except urllib.error.HTTPError as exc:
        with exc:
            status, raw = exc.code, exc.read(max_body + 1) if exc.fp else b""
    except Exception as exc:
        return ApiResult(error=f"{type(exc).__name__}: {str(exc)[:160]}", ms=round((time.monotonic() - start) * 1000))
    ms = round((time.monotonic() - start) * 1000)
    if len(raw) > max_body:
        return ApiResult(status=status, ms=ms, body=None, error=f"response larger than {max_body} bytes (not compared)")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = raw[:2000].decode("utf-8", "replace")
    return ApiResult(status=status, ms=ms, body=body)


def _fill_obj(obj: Any, variables: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        whole = re.fullmatch(r"\{(\w+)\}", obj)
        if whole and whole.group(1) in variables:
            return variables[whole.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(variables.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: _fill_obj(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill_obj(v, variables) for v in obj]
    return obj


def json_path(data: Any, path: str) -> Any:
    """Minimal JSONPath: $.a.b[0].c"""
    cur = data
    for part in re.findall(r"\.([^.\[\]]+)|\[(\d+)\]", path[1:] if path.startswith("$") else "." + path):
        key, index = part
        try:
            cur = cur[int(index)] if index else cur[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return _MISSING
    return cur


_MISSING = object()


# --- comparison ----------------------------------------------------------------------------------------------

def sensitive_fields(data: Any) -> set:
    """JSON paths whose key names a secret (password, token, card number...): names only, never values."""
    return {p for p in flatten(data) if SENSITIVE_FIELD.match(p.rsplit(".", 1)[-1].replace("[]", ""))}


def flatten(data: Any, prefix: str = "$", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Leaf paths -> values; list items collapse to [] (first item) so different list lengths still line up."""
    out = {} if out is None else out
    if isinstance(data, dict):
        for k, v in data.items():
            flatten(v, f"{prefix}.{k}", out)
    elif isinstance(data, list):
        out[f"{prefix}.length"] = len(data)
        if data:
            flatten(data[0], f"{prefix}[]", out)
    else:
        out[prefix] = data
    return out


def _type(v: Any) -> str:
    return "null" if v is None else ("bool" if isinstance(v, bool) else ("number" if isinstance(v, (int, float)) else type(v).__name__))


def compare_bodies(before: Any, after: Any, normalize: List[str]) -> List[str]:
    """Differences between two JSON bodies. Timestamps/UUIDs and configured fields are dynamic; nothing else is."""
    if not isinstance(before, (dict, list)) or not isinstance(after, (dict, list)):
        return [] if before == after or (isinstance(before, str) and isinstance(after, str)) else ["body changed"]
    fb, fa = flatten(before), flatten(after)
    diffs = []
    for path, bv in fb.items():
        leaf = path.rsplit(".", 1)[-1].replace("[]", "")
        if path not in fa:
            diffs.append(f"{path} is missing")
            continue
        av = fa[path]
        if path.endswith(".length"):
            if bv and not av:
                diffs.append(f"{path[:-7]} is now empty (was {bv} items)")
            continue
        if bv is not None and av is not None and _type(bv) != _type(av):
            diffs.append(f"{path} changed type {_type(bv)} -> {_type(av)}")
        elif bv != av and leaf not in normalize and not (isinstance(bv, str) and DYNAMIC_VALUE.match(bv)
                                                         and isinstance(av, str) and DYNAMIC_VALUE.match(av)):
            diffs.append(f"{path} changed {_short(bv)} -> {_short(av)}")
    return diffs


def _short(v: Any) -> str:
    text = json.dumps(v) if not isinstance(v, str) else repr(v)
    return redact_secrets(text[:40])


def validate_schema(data: Any, schema: Optional[Dict[str, Any]], path: str = "$", depth: int = 0) -> List[str]:
    """Contract check: required properties present and documented types respected (a deliberately small subset)."""
    if not schema or depth > 6:
        return []
    problems = []
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    nullable = schema.get("nullable") or "null" in types
    if data is None:
        return [] if nullable or not types else [f"{path} is null"]
    expected = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    real = [x for x in types if x != "null"]
    if real and not any(isinstance(data, expected.get(x, object)) and not (x in ("integer", "number") and isinstance(data, bool))
                        for x in real):
        return [f"{path} should be {'/'.join(real)}, got {_type(data)}"]
    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                problems.append(f"{path}.{req} is required but missing")
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                problems += validate_schema(data[k], sub, f"{path}.{k}", depth + 1)
    if isinstance(data, list) and data and schema.get("items"):
        problems += validate_schema(data[0], schema["items"], f"{path}[0]", depth + 1)
    return problems


def check_expectations(case: ApiCase, res: ApiResult) -> Tuple[List[str], str]:
    """(failures, source) against the strongest expectation available: explicit > contract > rule of thumb."""
    if res.error:
        return [res.error], "response"
    failures = list(res.step_failures)
    source = "explicit assertion" if case.kind == "explicit" else ("API contract" if case.expect_status else "")
    if case.expect_status and res.status not in case.expect_status and case.kind != "permission":
        failures.append(f"status {res.status}, expected {'/'.join(map(str, case.expect_status))}")
    for p, want in case.expect_body.items():
        got = json_path(res.body, p)
        if got is _MISSING:
            failures.append(f"{p} missing (expected {_short(want)})")
        elif got != want:
            failures.append(f"{p} is {_short(got)}, expected {_short(want)}")
    for f in case.expect_fields:
        if json_path(res.body, f if f.startswith("$") else "$." + f) is _MISSING:
            failures.append(f"field {f} missing")
    if case.max_ms and res.ms > case.max_ms:
        failures.append(f"took {res.ms} ms, limit {case.max_ms} ms")
    if case.assertions:
        context = {"response": {"status": res.status, "body": res.body, "ms": res.ms},
                   "request": {"body": case.body, "query": case.query, "path": case.path}, "vars": dict(res.vars)}
        failures += [f for f in (assertions.check(expr, context) for expr in case.assertions) if f]
    ep = case.endpoint
    if ep and res.status is not None and ep.responses and case.kind in ("valid", "explicit"):
        documented = ep.responses.get(str(res.status))
        if str(res.status) not in ep.responses and "default" not in ep.responses and res.status >= 500:
            failures.append(f"status {res.status} is not a documented response")
        elif documented:
            failures += validate_schema(res.body, documented)[:5]
            source = source or "API contract"
    if case.kind == "invalid" and not case.expect_status and res.status is not None and res.status < 400:
        failures.append(f"accepted invalid input ({case.name}) with {res.status}")
        source = source or "input validation"
    if case.kind == "permission" and res.status is not None:
        who = case.name.split(" ")[0]
        if case.allowed and res.status in (401, 403, 404, 405):
            failures.append(f"{who} should be allowed to {case.method} but got {res.status}")
        elif not case.allowed and res.status < 400:
            failures.append(f"{who} must not be able to {case.method} this, but got {res.status} (privilege escalation)")
        source = "permission matrix"
    if case.kind == "other_user" and res.status is not None and res.status < 300:
        failures.append(f"another test user ({case.identity}) can read this resource ({res.status})")
        source = source or "authorization"
    if case.kind == "unauth" and res.status is not None and res.status < 300 and (case.expect_status or ep and ep.requires_auth is not False):
        failures.append(f"responded {res.status} without credentials")
        source = source or "authentication"
    if res.status is not None and res.status >= 500:
        failures.append(f"server error {res.status}")
        source = source or "server error"
    # Secrets in a response body, one finding per field (names only) so a newly exposed field counts as new
    exposed = [f"response exposes sensitive field {p}" for p in sorted(sensitive_fields(res.body))]
    if exposed:
        failures += exposed
        source = source or "sensitive data"
    return failures, source or "baseline behaviour"


@dataclass
class CaseOutcome:
    case: ApiCase
    base: Optional[ApiResult]
    cand: Optional[ApiResult]
    classification: str  # regression | removed | potential_regression | existing_failure | expected_difference | environment | passed | not_tested
    details: List[str] = field(default_factory=list)
    source: str = ""
    refusal: Optional[str] = None


def classify(case: ApiCase, base: Optional[ApiResult], cand: ApiResult, normalize: List[str]) -> CaseOutcome:
    cand_fail, source = check_expectations(case, cand)
    if base is None:  # single environment (check / staging / production): contract and assertions only
        if cand.error:
            return CaseOutcome(case, None, cand, "environment", [cand.error], "response")
        return CaseOutcome(case, None, cand, "regression" if cand_fail else "passed", cand_fail, source)
    if cand.error and base.error:
        return CaseOutcome(case, base, cand, "environment", [f"both versions: {cand.error}"], "response")
    if cand.error:
        return CaseOutcome(case, base, cand, "regression", [f"candidate did not respond ({cand.error}); baseline answered {base.status}"], "response")
    if case.guard and cand_fail:  # a pinned, previously fixed bug is back: no baseline excuse
        return CaseOutcome(case, base, cand, "regression", ["previously fixed bug is back: " + cand_fail[0]] + cand_fail[1:], source)
    # The endpoint answered before and is gone now: a feature removed, unless the developer pinned the change
    if (case.kind in ("valid", "explicit") and base.status is not None and base.status < 400
            and cand.status in (404, 405, 410) and not (case.expect_status and cand.status in case.expect_status)):
        return CaseOutcome(case, base, cand, "removed", [f"endpoint removed: answered {base.status} before, now {cand.status}"],
                           "baseline behaviour")
    base_fail, _ = check_expectations(case, base)
    new = [f for f in cand_fail if f not in base_fail]
    if new and not base.error:
        return CaseOutcome(case, base, cand, "regression", new, source)
    if cand_fail:
        return CaseOutcome(case, base, cand, "existing_failure", cand_fail, source)
    diffs = []
    if base.status != cand.status:
        diffs.append(f"status {base.status} -> {cand.status}")
    diffs += compare_bodies(base.body, cand.body, normalize)[:8]
    if base.ms and cand.ms > max(2 * base.ms, base.ms + 500):
        diffs.append(f"slower: {base.ms} ms -> {cand.ms} ms")
    if not diffs:
        return CaseOutcome(case, base, cand, "passed", [], source)
    # Differences that pass every check are only suspicious, never confirmed. A case's own expectations passing
    # makes the difference intended; a case with no expectations proves nothing either way.
    pinned = case.kind == "explicit" and (case.expect_status or case.expect_body or case.expect_fields or case.assertions)
    category = "expected_difference" if pinned else "potential_regression"
    return CaseOutcome(case, base, cand, category, diffs, "baseline behaviour")


# --- running -------------------------------------------------------------------------------------------------

@dataclass
class RunLimits:
    max_requests: int = 200
    rate_per_second: float = 10.0
    timeout: float = 10.0
    max_body_bytes: int = 1_000_000


class Runner:
    """Sends cases to one or two deployments with a global request budget and rate limit (one request at a time)."""

    def __init__(self, limits: RunLimits, identities: Optional[Dict[str, Dict[str, str]]] = None):
        self.limits = limits
        self.identities = identities or {}  # name -> headers for cases sent `as` another test identity
        self.sent = 0
        self._last = 0.0

    def _throttle(self) -> bool:
        if self.sent >= self.limits.max_requests:
            return False
        wait = 1.0 / max(self.limits.rate_per_second, 0.01) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.sent += 1
        return True

    def run(self, base_url: str, case: ApiCase, auth: Dict[str, str]) -> Optional[ApiResult]:
        """None when the request budget is spent."""
        variables: Dict[str, str] = {}
        result: Optional[ApiResult] = None
        for step in [case] + case.steps:
            if not self._throttle():
                return result
            creds = self.identities.get(step.identity, auth) if step.identity else auth
            if not step.with_auth:
                creds = {}
            r = http_call(base_url, step, creds, self.limits.timeout, self.limits.max_body_bytes, variables)
            for name, path in step.save.items():
                value = json_path(r.body, path)
                if value is not _MISSING:
                    variables[name] = str(value)
            if result is None:
                result = r
            elif step is not case:
                r.vars = dict(variables)
                fails, _ = check_expectations(step, r)
                result.step_failures += [f"step '{step.name}': {f}" for f in fails]
            if r.error:
                break
        if result is not None:
            result.vars = dict(variables)
        return result


def run_suite(cases: List[ApiCase], policy: Policy, limits: RunLimits, cand_url: str, cand_auth: Dict[str, str],
              base_url: Optional[str] = None, base_auth: Optional[Dict[str, str]] = None,
              normalize: Optional[List[str]] = None,
              identities: Optional[Dict[str, Dict[str, str]]] = None) -> List[CaseOutcome]:
    runner = Runner(limits, identities)
    outcomes = []
    for case in cases:
        refusal = policy.refusal(case, identities or {})
        if refusal:
            outcomes.append(CaseOutcome(case, None, None, "not_tested", [], refusal=refusal))
            continue
        base = runner.run(base_url, case, base_auth or {}) if base_url else None
        cand = runner.run(cand_url, case, cand_auth)
        if cand is None or (base_url and base is None):
            outcomes.append(CaseOutcome(case, None, None, "not_tested", [], refusal="request budget (api_tests.max_requests) used up"))
            continue
        outcomes.append(classify(case, base, cand, normalize or []))
    return outcomes


# --- selection, reporting ------------------------------------------------------------------------------------

def select_endpoints(endpoints: List[Endpoint], mode: str, changed_files: List[str], smoke: int = 10) -> List[Endpoint]:
    """smoke: a few safe GETs; affected: endpoints whose files or path words changed, plus smoke; full: everything."""
    if mode == "full":
        return endpoints
    gets = [e for e in endpoints if e.method == "GET" and "{" not in e.path or e.sample_path]
    health = [e for e in gets if re.search(r"health|status|ping|version", e.path)]
    picked = list(dict.fromkeys(health + gets))[:smoke]
    if mode == "affected":
        words = {w for f in changed_files for w in re.split(r"[/_.\-]", f.lower()) if len(w) > 3}
        hit = [e for e in endpoints if (e.source_file and e.source_file in changed_files)
               or any(w in e.path.lower() for w in words)]
        # Impact unknown (e.g. only OpenAPI routes, no file mapping): broaden instead of silently skipping
        picked = list(dict.fromkeys(picked + (hit if hit else endpoints)))
    return picked


def sanitize_url(path: str, query: Dict[str, Any]) -> str:
    q = {k: ("***" if SENSITIVE_KEY.search(k) else v) for k, v in query.items()}
    return redact_secrets(path + ("?" + urllib.parse.urlencode(q) if q else ""))


def sanitize_body(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: ("***" if SENSITIVE_KEY.search(str(k)) else sanitize_body(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [sanitize_body(v) for v in body[:5]]
    return redact_secrets(body) if isinstance(body, str) else body


def to_findings(outcomes: List[CaseOutcome], endpoints_discovered: int, env_label: str) -> List[Finding]:
    findings = []
    for o in outcomes:
        if o.classification in ("passed", "not_tested"):
            continue
        c = o.case
        payload = json.dumps(sanitize_body(c.body))[:300] if c.body is not None and c.body_kind != "raw" else (c.body if c.body_kind == "raw" else "")
        category, critical = {
            "regression": ("REGRESSION", True),
            "removed": ("REMOVED_FUNCTIONALITY", False),
            "potential_regression": ("POSSIBLE_REGRESSION", False),
            "existing_failure": ("NOTICE", False),
            "expected_difference": ("EXPECTED_CHANGE", False),
            "environment": ("BLOCKED", False),
        }[o.classification]
        def outcome(r):
            return "not run" if r is None else (r.error or f"{r.status} in {r.ms} ms")
        findings.append(Finding(
            category=category,
            target=f"API {c.method} {sanitize_url(c.path, {})} [{c.name}]",
            before_behavior=f"baseline: {outcome(o.base)}" if o.base else f"{env_label}",
            after_behavior="; ".join(o.details[:6]),
            expected_behavior=f"from {o.source}" + (f": status {'/'.join(map(str, c.expect_status))}" if c.expect_status else ""),
            actual_result=f"candidate: {outcome(o.cand)}",
            confidence={"explicit assertion": 0.95, "API contract": 0.9}.get(o.source, 0.7),
            recommended_action={"regression": "Fix before pushing: this request now fails a check it passed before.",
                                 "removed": "This endpoint worked before and is gone now: restore it, or confirm the removal is intended.",
                                 "potential_regression": "Differs from the baseline but breaks no documented rule: confirm it is intended (add an explicit case to pin it).",
                                 "existing_failure": "Already failing on the baseline: not caused by this change.",
                                 "expected_difference": "Changed, and matches your explicit expectations.",
                                 "environment": "Not a code regression: the test environment did not respond."}[o.classification],
            is_critical=critical,
            reproduction_steps=[f"{c.method} {sanitize_url(c.path, c.query)}"] + ([f"body: {payload}"] if payload else [])
            + ([f"then: {s.method} {sanitize_url(s.path, s.query)}" for s in c.steps[:3]]),
        ))
    tested = sum(1 for o in outcomes if o.classification != "not_tested")
    skipped = [o for o in outcomes if o.classification == "not_tested"]
    reasons = sorted({o.refusal for o in skipped if o.refusal})
    findings.append(Finding(
        category="NOTICE",
        target="API coverage",
        before_behavior=f"{endpoints_discovered} endpoint(s) discovered",
        after_behavior=f"{tested} request case(s) run, {len(skipped)} not run" + (": " + "; ".join(reasons) if reasons else ""),
        expected_behavior="Discovered, tested and untested endpoints are reported separately",
        actual_result=f"{tested} run", confidence=1.0,
        recommended_action="Add explicit api_tests.cases for endpoints that were not run." if skipped else "No action required.",
    ))
    return findings


# --- environment safety --------------------------------------------------------------------------------------

LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "db", "database", "postgres", "postgresql", "mysql",
                  "mariadb", "mongo", "mongodb", "redis", "host.docker.internal"}
DB_URL_KEYS = re.compile(r"^(DATABASE_URL|DB_URL|.*_DATABASE_URL|POSTGRES_URL|MONGO(DB)?_URI|MONGO(DB)?_URL|MYSQL_URL)$", re.I)
DB_HOST_KEYS = re.compile(r"^(DB_HOST|DATABASE_HOST|POSTGRES_HOST|PGHOST|MYSQL_HOST|MONGO_HOST)$", re.I)
ENV_NAME_KEYS = re.compile(r"^(ENV|ENVIRONMENT|APP_ENV|NODE_ENV|DJANGO_ENV|FLASK_ENV|RAILS_ENV|STAGE)$", re.I)


def read_env_files(root: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for f in sorted(root.glob(".env*")):
        if f.is_file() and not f.name.endswith((".example", ".sample", ".template")):
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                key, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and not key.startswith("#"):
                    values.setdefault(key.strip(), value.strip().strip("'\""))
    return values


def remote_database(env: Dict[str, str]) -> Optional[str]:
    """Why this environment looks like it points at a shared/remote (possibly production) database, or None."""
    for k, v in env.items():
        if DB_URL_KEYS.match(k) and v:
            if v.startswith("sqlite") or "://" not in v:
                continue
            host = (urllib.parse.urlparse(v).hostname or "").lower()
            if host not in LOCAL_DB_HOSTS:
                return f"{k} points at {host}"
        if DB_HOST_KEYS.match(k) and v.lower() not in LOCAL_DB_HOSTS:
            return f"{k}={v}"
        if ENV_NAME_KEYS.match(k) and re.search(r"prod", v, re.I):
            return f"{k}={v}"
    return None
