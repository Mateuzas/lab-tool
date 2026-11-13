"""Synthetic API records (no real program data). Shapes follow docs/research/intigriti-api.md."""

import copy
import json

FX = {"EUR": 1, "USD": 0.92, "GBP": 1.17}


def list_record(pid="00000000-0000-0000-0000-000000000001", handle="acme", name="Acme",
                status="Open", conf="Public", min_b=50, max_b=10000, currency="EUR", company="acmecorp"):
    return {
        "id": pid, "handle": handle, "name": name, "following": False,
        "minBounty": {"value": float(min_b), "currency": currency},
        "maxBounty": {"value": float(max_b), "currency": currency},
        "confidentialityLevel": {"id": 4, "value": conf},
        "status": {"id": 3, "value": status},
        "type": {"id": 1, "value": "Bug Bounty"},
        "webLinks": {"detail": "https://app.intigriti.com/auth/dashboard?redirect=/programs/%s/%s/detail"
                               % (company, handle)},
        "industry": "Software",
    }


def entry(type_, endpoint, tier, desc=None):
    return {"id": "e-" + endpoint, "type": {"id": 1, "value": type_}, "endpoint": endpoint,
            "tier": {"id": 1, "value": tier}, "description": desc, "requiredSkills": []}


def detail(record, entries=None, rps=5, safe_harbour=True, intigriti_me=True, user_agent=None,
           request_header="X-Intigriti-Username: {Username}", domains_version="dom-v1",
           domains_created=1790000000, rules_version="roe-v1", rules_created=1780000000, text="Be nice."):
    if entries is None:
        entries = [entry("Wildcard", "*.acme.example", "Tier 1"),
                   entry("Url", "https://app.acme.example/api", "Tier 2"),
                   entry("Url", "www.acme.example", "No Bounty"),
                   entry("Url", "status.acme.example", "Out Of Scope", "third party")]
    d = copy.deepcopy(record)
    for k in ("minBounty", "maxBounty"):
        d.pop(k, None)
    d["domains"] = {"id": domains_version, "createdAt": domains_created, "content": entries}
    d["rulesOfEngagement"] = {"attachments": [], "id": rules_version, "createdAt": rules_created,
                              "content": {"description": text, "safeHarbour": safe_harbour,
                                          "testingRequirements": {"intigritiMe": intigriti_me,
                                                                  "automatedTooling": rps,
                                                                  "userAgent": user_agent,
                                                                  "requestHeader": request_header}}}
    return d


def activity(pid, created, type_id=3, value="New program status available"):
    return {"programId": pid, "activity": {}, "type": {"id": type_id, "value": value},
            "createdAt": created, "following": False}


def body(obj):
    return json.dumps(obj).encode("utf-8")
