"""Money out to Google Cloud, per project, from the BigQuery billing export.

The Cloud Billing API does not report spend; only the billing export to
BigQuery does. So this reads `gcp_billing_export_v1_*` (configured, or found
in the service account's own project), sums cost net of credits per project,
and maps projects to companies. Read-only: one SELECT, nothing else.

Signed with the service-account key through `openssl` (the deck's venv has no
crypto library); the key goes to openssl on stdin and never touches argv.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
BQ = "https://bigquery.googleapis.com/bigquery/v2/projects/"
SQL = ("SELECT project.id AS p, currency, SUM(cost) + SUM(IFNULL((SELECT "
       "SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net FROM `{table}` "
       "WHERE usage_start_time >= TIMESTAMP_SECONDS(@since) GROUP BY p, currency")
HOW = ("In Google Cloud Billing, turn on 'Export to BigQuery' (Standard usage "
       "cost) into a dataset, and give the deck's service account BigQuery Data "
       "Viewer on it and BigQuery Job User on its project.")


class CloudError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail or reason


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def token(sa: dict) -> str:
    now = int(time.time())
    head = _b64(b'{"alg":"RS256","typ":"JWT"}')
    claim = _b64(json.dumps({"iss": sa["client_email"], "scope": SCOPE,
                             "aud": "https://oauth2.googleapis.com/token",
                             "iat": now, "exp": now + 600}).encode())
    body = head + b"." + claim
    with tempfile.NamedTemporaryFile() as data:
        data.write(body); data.flush()
        signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", "/dev/stdin",
                                 data.name], input=sa["private_key"].encode(),
                                capture_output=True, timeout=20)
    if signed.returncode or not signed.stdout:
        raise CloudError("sign_failed", "openssl could not sign with the key")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": (body + b"." + _b64(signed.stdout)).decode()})
    if r.status_code != 200:
        raise CloudError("auth_failed", str(r.json().get("error_description") or r.status_code))
    return r.json()["access_token"]


def _get(tok: str, url: str) -> dict:
    r = httpx.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    body = r.json()
    if r.status_code != 200:
        raise CloudError(str(r.status_code), str((body.get("error") or {}).get("message"))[:200])
    return body


def list_tables(tok: str, project: str) -> list[str]:
    """`dataset.table` for every billing export table in `project`."""
    out = []
    for ds in _get(tok, f"{BQ}{project}/datasets").get("datasets") or []:
        dsid = ds["datasetReference"]["datasetId"]
        for t in _get(tok, f"{BQ}{project}/datasets/{dsid}/tables").get("tables") or []:
            tid = t["tableReference"]["tableId"]
            if tid.startswith("gcp_billing_export_v1_"):
                out.append(f"{dsid}.{tid}")
    return out


def query(tok: str, project: str, sql: str, params: dict) -> list[dict]:
    r = httpx.post(f"{BQ}{project}/queries", headers={"Authorization": f"Bearer {tok}"},
                   timeout=60, json={"query": sql, "useLegacySql": False,
                                     "timeoutMs": 50000, "queryParameters": [
                                         {"name": k, "parameterType": {"type": "INT64"},
                                          "parameterValue": {"value": str(int(v))}}
                                         for k, v in params.items()]})
    body = r.json()
    if r.status_code != 200:
        raise CloudError(str(r.status_code), str((body.get("error") or {}).get("message"))[:200])
    names = [f["name"] for f in body.get("schema", {}).get("fields", [])]
    return [dict(zip(names, (c.get("v") for c in row["f"]))) for row in body.get("rows") or []]


def fetch(sa_file, table: str, *, since: float, projects: dict[str, str],
          token=token, list_tables=list_tables, query=query) -> dict:
    """Spend since `since` per company. Never raises; says what is missing."""
    if not sa_file or not Path(sa_file).is_file():
        return {"state": "missing", "detail": "No Google Cloud service account "
                "on the deck. " + HOW}
    try:
        sa = json.loads(Path(sa_file).read_text())
        tok = token(sa)
        home = sa.get("project_id") or ""
        if not table:
            found = list_tables(tok, home)
            if not found:
                return {"state": "missing", "detail": "No billing export found. " + HOW}
            table = f"{home}.{found[0]}"
        rows = query(tok, table.split(".")[0] if table.count(".") == 2 else home,
                     SQL.format(table=table), {"since": since})
    except CloudError as exc:
        return {"state": "error", "detail": f"{exc.reason}: {exc.detail}"}
    except (OSError, ValueError, KeyError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        return {"state": "error", "detail": f"unreadable: {type(exc).__name__}"}
    by_company: dict = {}
    currency = ""
    for row in rows:
        who = projects.get(str(row.get("p") or ""))
        by_company[who] = round(by_company.get(who, 0) + float(row.get("net") or 0), 2)
        currency = currency or str(row.get("currency") or "")
    return {"state": "connected", "table": table, "currency": currency,
            "by_company": by_company}
