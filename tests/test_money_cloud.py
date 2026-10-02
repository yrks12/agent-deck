"""Money out to Google Cloud, per project, from the BigQuery billing export."""

from __future__ import annotations

import json

from server.money import cloud

PROJECTS = {"acme-prod-1": "Acme", "globex-prod": "Globex"}


def _sa(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account", "project_id": "ops-1",
                             "client_email": "a@b", "private_key": "KEY"}))
    return p


def test_no_service_account_says_connect(tmp_path):
    got = cloud.fetch(None, "", since=0, projects=PROJECTS)
    assert got["state"] == "missing" and "service account" in got["detail"]


def test_no_export_found_says_turn_on_the_billing_export(tmp_path):
    got = cloud.fetch(_sa(tmp_path), "", since=0, projects=PROJECTS,
                      token=lambda sa: "t", list_tables=lambda tok, project: [])
    assert got["state"] == "missing" and "billing export" in got["detail"]


def test_costs_are_net_of_credits_and_mapped_to_companies(tmp_path):
    seen = {}

    def query(tok, project, sql, params):
        seen.update(project=project, sql=sql, params=params)
        return [{"p": "acme-prod-1", "currency": "GBP", "net": 12.345},
                {"p": "globex-prod", "currency": "GBP", "net": 3.0},
                {"p": "stray-proj", "currency": "GBP", "net": 1.0}]
    got = cloud.fetch(_sa(tmp_path), "", since=100, projects=PROJECTS,
                      token=lambda sa: "t",
                      list_tables=lambda tok, project: [
                          "billing.gcp_billing_export_v1_0A1B2C"],
                      query=query)
    assert got["state"] == "connected"
    assert got["table"] == "ops-1.billing.gcp_billing_export_v1_0A1B2C"
    assert got["by_company"] == {"Acme": 12.35, "Globex": 3.0, None: 1.0}
    assert got["currency"] == "GBP"
    assert "credits" in seen["sql"] and seen["params"]["since"] == 100
    assert seen["project"] == "ops-1"


def test_a_google_error_is_reported_not_raised(tmp_path):
    def boom(sa):
        raise cloud.CloudError("permission", "BigQuery Data Viewer is missing")
    got = cloud.fetch(_sa(tmp_path), "x.y.z", since=0, projects=PROJECTS, token=boom)
    assert got["state"] == "error" and "Data Viewer" in got["detail"]
