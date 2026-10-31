#!/usr/bin/env python3
"""Service-contract emitter - OpenStack reference implementation.

Emits the two runtime signals the service integration contract
(serviceContract stanza, values.yaml) requires from every service:

  MODE=usage   normalized UsageRecord rows collected from the OpenStack APIs
               (Nova limits + hypervisor statistics, Cinder quota usage +
               scheduler pools, Neutron floating IPs), one row per
               project x metric per aligned window, tagged with the Keystone
               project tags.
  MODE=health  one normalized row per catalog service, probed from the
               Keystone service catalog and mapped onto exactly
               OK | Warning | Critical | Unknown, plus a consolidated
               "_service" row (worst component wins).

Both are written to ClickHouse over the HTTP interface as JSONEachRow
inserts. record_id reproduces showback's deterministicID(org, tenant,
service, resource, metric, window) byte-for-byte (sha256 over
NUL-terminated parts, first 32 hex chars), so re-runs inside the same
window are idempotent (ReplacingMergeTree replaces, never duplicates) and
a record inserted directly gets the same id showback would compute for it.

Stdlib-only on purpose: runs on a plain python:3-alpine image.
Configuration is environment-only (see templates/contract-emitter.yaml).
"""
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = os.environ.get("SERVICE_NAME", "openstack")
ORG = os.environ.get("ORG", "default")
SOURCE = os.environ.get("SOURCE", "openstack-blueprint")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_interval(s):
    """'90s' | '5m' | '1h' -> seconds."""
    return int(s[:-1]) * {"s": 1, "m": 60, "h": 3600}[s[-1]]


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def fmt_rfc3339(ts):
    """UTC RFC3339 ('2026-01-02T15:04:05Z') - the exact string showback's
    deterministicID hashes for window_start (Go time.RFC3339Nano renders
    whole seconds without a fractional part)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def deterministic_id(*parts):
    """showback's deterministicID: sha256 over NUL-terminated parts, first
    32 hex chars. Call with (org, tenant, service, resource, metric,
    window) so a row inserted directly into ClickHouse and the same logical
    record ingested through the showback API converge on one record_id."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


def split_tags(raw):
    """Keystone project tags are flat strings; the platform convention is
    'key:value'. Split on the first ':' into real key->value Map entries so
    per-tag GROUP BY (showback by-tag rollups, tag governance) works.
    Documented fallback: a bare tag without ':' becomes {tag: ''}."""
    tags = {}
    for t in raw:
        k, sep, v = t.partition(":")
        tags[k] = v if sep else ""
    return tags


def ssl_context(url):
    if url.startswith("https") and os.environ.get("TLS_VERIFY", "true").lower() == "false":
        return ssl._create_unverified_context()
    return None


def request(method, url, token=None, body=None, headers=None, timeout=15):
    data = None
    req_headers = {"Content-Type": "application/json"}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    if token:
        req_headers["X-Auth-Token"] = token
    req_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    resp = urllib.request.urlopen(req, timeout=timeout, context=ssl_context(url))
    return resp.status, dict(resp.headers), resp.read()


def get_json(url, token):
    _, _, raw = request("GET", url, token=token)
    return json.loads(raw)


# --------------------------------------------------------------------------
# Keystone
# --------------------------------------------------------------------------
def keystone_auth():
    auth_url = os.environ["OS_AUTH_URL"].rstrip("/")
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": os.environ["OS_USERNAME"],
                        "domain": {"name": os.environ.get("OS_USER_DOMAIN_NAME", "default")},
                        "password": os.environ["OS_PASSWORD"],
                    }
                },
            },
            "scope": {
                "project": {
                    "name": os.environ.get("OS_PROJECT_NAME", "admin"),
                    "domain": {"name": os.environ.get("OS_PROJECT_DOMAIN_NAME", "default")},
                }
            },
        }
    }
    _, headers, raw = request("POST", auth_url + "/auth/tokens", body=body)
    token = headers["X-Subject-Token"]
    catalog = json.loads(raw)["token"].get("catalog", [])
    return auth_url, token, catalog


def endpoint_url(catalog, svc_type):
    interface = os.environ.get("OS_INTERFACE", "internal")
    region = os.environ.get("OS_REGION_NAME", "")
    for svc in catalog:
        if svc.get("type") != svc_type:
            continue
        for ep in svc.get("endpoints", []):
            if ep.get("interface") != interface:
                continue
            if region and ep.get("region") not in (region, None):
                continue
            return ep["url"].rstrip("/")
    return None


# --------------------------------------------------------------------------
# Usage (UsageRecord shape: contract `usage.metrics`)
# --------------------------------------------------------------------------
def collect_usage(auth_url, token, catalog):
    interval = parse_interval(os.environ.get("INTERVAL", "5m"))
    wstart = int(time.time()) // interval * interval
    wend = wstart + interval
    rows = []

    def row(metric, qty, unit, resource_id, tenant, tags=None):
        rid = deterministic_id(ORG, tenant, SERVICE, resource_id, metric, fmt_rfc3339(wstart))
        rows.append({
            "record_id": rid,
            "org": ORG,
            "tenant": tenant,
            "service": SERVICE,
            "resource_id": resource_id,
            "metric": metric,
            "quantity": float(qty),
            "unit": unit,
            "window_start": fmt(wstart),
            "window_end": fmt(wend),
            "tags": tags or {},
            "source": SOURCE,
        })

    ks = endpoint_url(catalog, "identity") or auth_url
    nova = endpoint_url(catalog, "compute")
    cinder = endpoint_url(catalog, "volumev3") or endpoint_url(catalog, "block-storage")
    neutron = endpoint_url(catalog, "network")

    projects = get_json(ks + "/projects", token).get("projects", [])

    fips = {}
    if neutron:
        try:
            for fip in get_json(neutron + "/v2.0/floatingips", token).get("floatingips", []):
                pid = fip.get("project_id") or fip.get("tenant_id") or ""
                fips[pid] = fips.get(pid, 0) + 1
        except Exception as e:  # noqa: BLE001 - collectors are best-effort
            log(f"WARN neutron floatingips: {e}")

    for p in projects:
        pid, name = p["id"], p["name"]
        tags = split_tags(p.get("tags", []))
        if nova:
            try:
                ab = get_json(f"{nova}/limits?tenant_id={pid}", token)["limits"]["absolute"]
                row("instances.count", ab.get("totalInstancesUsed", 0), "count", pid, name, tags)
                row("vcpu.used", ab.get("totalCoresUsed", 0), "cores", pid, name, tags)
                row("ram.used", ab.get("totalRAMUsed", 0), "MiB", pid, name, tags)
            except Exception as e:
                log(f"WARN nova limits [{name}]: {e}")
        if cinder:
            try:
                qs = get_json(f"{cinder}/os-quota-sets/{pid}?usage=True", token)["quota_set"]
                gb = qs.get("gigabytes")
                if isinstance(gb, dict):
                    row("storage.used", gb.get("in_use", 0), "GiB", pid, name, tags)
            except Exception as e:
                log(f"WARN cinder quota [{name}]: {e}")
        if pid in fips:
            row("floating_ips.count", fips[pid], "count", pid, name, tags)

    # Capacity metrics are org-level (tenant = "").
    if nova:
        try:
            st = get_json(f"{nova}/os-hypervisors/statistics", token)["hypervisor_statistics"]
            row("vcpu.capacity", st.get("vcpus", 0), "cores", "hypervisors", "")
            row("ram.capacity", st.get("memory_mb", 0), "MiB", "hypervisors", "")
        except Exception as e:
            log(f"WARN hypervisor statistics: {e}")
    if cinder:
        try:
            pools = get_json(f"{cinder}/scheduler-stats/get_pools?detail=True", token).get("pools", [])
            cap = sum(float(p.get("capabilities", {}).get("total_capacity_gb") or 0) for p in pools)
            if pools:
                row("storage.capacity", cap, "GiB", "cinder-pools", "")
        except Exception as e:
            log(f"WARN cinder pools: {e}")

    return rows


# --------------------------------------------------------------------------
# Health (contract `health`: exactly OK | Warning | Critical | Unknown)
# --------------------------------------------------------------------------
RANK = {"OK": 0, "Warning": 1, "Unknown": 2, "Critical": 3}


def probe(url):
    try:
        status, _, _ = request("GET", url, timeout=5)
    except urllib.error.HTTPError as e:
        status = e.code
    except (socket.timeout, TimeoutError):
        return "Unknown", "timeout"
    except Exception as e:
        return "Critical", type(e).__name__
    if status < 400 or status in (401, 403):
        return "OK", f"http {status}"
    if status >= 500:
        return "Critical", f"http {status}"
    return "Warning", f"http {status}"


def collect_health(auth_url, token, catalog):
    del token
    interval = parse_interval(os.environ.get("INTERVAL", "1m"))
    wstart = int(time.time()) // interval * interval
    checked_at = fmt(int(time.time()))
    rows = []

    def row(component, status, reason):
        # Same deterministic scheme as usage rows (tenant is always "" for
        # health): org-qualified, so two orgs emitting the same service
        # into one table never collide.
        rid = deterministic_id(ORG, "", SERVICE, component, fmt_rfc3339(wstart))
        rows.append({
            "record_id": rid,
            "org": ORG,
            "tenant": "",
            "service": SERVICE,
            "component": component,
            "status": status,
            "reason": reason,
            "checked_at": checked_at,
        })

    interface = os.environ.get("OS_INTERFACE", "internal")
    seen = False
    worst = "Unknown"
    for svc in catalog:
        url = None
        for ep in svc.get("endpoints", []):
            if ep.get("interface") == interface:
                url = ep["url"]
                break
        if not url:
            continue
        status, reason = probe(url)
        row(svc.get("type", svc.get("name", "unknown")), status, reason)
        worst = status if not seen or RANK[status] > RANK[worst] else worst
        seen = True
    if not seen:
        status, reason = probe(auth_url)
        row("identity", status, reason)
        worst = status
    row("_service", worst, "worst of components")
    return rows


# --------------------------------------------------------------------------
# ClickHouse (HTTP interface)
# --------------------------------------------------------------------------
def usage_database():
    return os.environ.get("CH_DATABASE", "showback")


def health_database():
    """Health rows live in their own database, and not by accident.

    showback owns the usage schema and migrates it; this emitter owns the health table and
    creates it. Pointing both at showback's database meant bootstrap issued CREATE TABLE
    inside a database that does not exist until showback has run -- so on any cluster
    without showback, which is currently every cluster, the half this emitter owns broke
    in order to make the half it does not own correct.
    """
    return os.environ.get("CH_HEALTH_DATABASE") or usage_database()


def ch_query(sql, body=None, database=None):
    base = os.environ["CH_URL"].rstrip("/")
    db = usage_database() if database is None else database
    params = {"query": sql}
    # An empty database means "do not pin one": CREATE DATABASE cannot run inside the
    # database it is creating.
    if db:
        params["database"] = db
    headers = {"Content-Type": "application/octet-stream"}
    if os.environ.get("CH_USERNAME"):
        headers["X-ClickHouse-User"] = os.environ["CH_USERNAME"]
        headers["X-ClickHouse-Key"] = os.environ.get("CH_PASSWORD", "")
    url = base + "/?" + urllib.parse.urlencode(params)
    return request("POST", url, body=body if body is not None else b"", headers=headers, timeout=30)


def ch_insert(table, rows, database=None):
    if not rows:
        log("nothing to insert")
        return
    db = usage_database() if database is None else database
    sql = f"INSERT INTO {db}.{table} FORMAT JSONEachRow"
    body = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    ch_query(sql, body=body, database=db)
    log(f"inserted {len(rows)} rows into {db}.{table}")


def ch_bootstrap_health_table():
    db = health_database()
    table = os.environ.get("CH_HEALTH_TABLE", "health_records")
    # Unpinned, because the database may not exist yet -- a query pinned to it would fail
    # before it could create it.
    ch_query(f"CREATE DATABASE IF NOT EXISTS {db}", database="")
    ch_query(
        f"CREATE TABLE IF NOT EXISTS {db}.{table} ("
        " record_id String,"
        " org LowCardinality(String),"
        " tenant LowCardinality(String),"
        " service LowCardinality(String),"
        " component LowCardinality(String),"
        " status Enum8('OK' = 1, 'Warning' = 2, 'Critical' = 3, 'Unknown' = 4),"
        " reason String,"
        " checked_at DateTime64(3, 'UTC') DEFAULT now64(3)"
        ") ENGINE = ReplacingMergeTree(checked_at)"
        " PARTITION BY toYYYYMM(checked_at)"
        " ORDER BY (org, tenant, service, component, record_id)"
    )


def main():
    mode = os.environ.get("MODE", "usage")
    auth_url, token, catalog = keystone_auth()
    if mode == "usage":
        rows = collect_usage(auth_url, token, catalog)
        ch_insert(os.environ.get("CH_USAGE_TABLE", "usage_records"), rows)
    elif mode == "health":
        if os.environ.get("CH_BOOTSTRAP_HEALTH_TABLE", "true").lower() == "true":
            ch_bootstrap_health_table()
        rows = collect_health(auth_url, token, catalog)
        ch_insert(os.environ.get("CH_HEALTH_TABLE", "health_records"), rows,
                  database=health_database())
    else:
        raise SystemExit(f"unknown MODE {mode!r} (usage|health)")


if __name__ == "__main__":
    main()
