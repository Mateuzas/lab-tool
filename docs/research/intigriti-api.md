# Intigriti Researcher API: field report (BE-1, 2026-11-13)

Explored with the owner's personal access token. Raw responses are in the (gitignored)
`workspace/bounty/discovery-20261113/`. Base URL: `https://api.intigriti.com/external/researcher`,
auth `Authorization: Bearer <token>`. Spec: `/swagger/v1/swagger.json` (public).

## Endpoints

| Endpoint | Params | Notes |
|---|---|---|
| `GET /v1/programs` | `statusId, typeId, following, limit, offset` | `{maxCount, records[]}`; `limit=500` returned all 233 in one call |
| `GET /v1/programs/{id}` | – | full program: domains (scope) + rules of engagement. **403** for programs you haven't joined (53 of 199 open: 24 Registered, 17 Application, 12 Public) |
| `GET /v1/programs/activities` | `createdSince, following, limit, offset` | 9018 events; 500 covered ~6 weeks |
| `GET /v1/programs/{id}/domains/{versionId}` | – | an older scope version |
| `GET /v1/programs/{id}/rules-of-engagements/{versionId}` | – | an older rules version |
| `GET /v1/payouts` | `statusId, createdSince, paidSince, limit, offset` | **your own** payouts: input for ranking feedback |

No rate-limit headers were returned. We call at ≤ 1 request/second.

## List record (`/v1/programs`)

`id, handle, name, following, minBounty{value,currency}, maxBounty{value,currency},
confidentialityLevel{id,value}, status{id,value}, type{id,value}, webLinks.detail, industry`

Snapshot: 233 programs; Open 199, Suspended 34; Public 174, Registered 36, Application 23; all
`type` = "Bug Bounty", but **82 have maxBounty 0** (effectively no bounty). Currencies EUR 172,
USD 59, GBP 2. maxBounty median 2,200, p75 5,000, p90 8,500, max 51,337.

## Detail (`/v1/programs/{id}`)

- `domains: {id (version), createdAt (unix s), content[]}`; each entry
  `{id, type{value}, endpoint, tier{value}, description, requiredSkills[]}`.
  - types: Url 1215, Wildcard 589, Other 154, Android 73, iOS 70, IpRange 56, SourceCode 29, Device 26
  - tiers: Tier 1 294, Tier 2 669, Tier 3 136, Tier 4 131, Tier 5 49, No Bounty 550, **Out Of Scope 383**
    (out of scope is a tier, not a separate list)
  - `createdAt` = when scope last changed: p25 73 days ago, median 175, p75 521.
- `rulesOfEngagement: {id, createdAt, attachments[], content{description (markdown), safeHarbour (bool),
  testingRequirements{intigritiMe, automatedTooling, userAgent, requestHeader}}}`
  - **`automatedTooling` is a requests-per-second limit** (int, nullable): 5 → 67 programs, null 28,
    2 → 20, 10 → 13, 1 → 7, 3 → 4, 20 → 4, 0 → 2, 50 → 1. Read 0 as "no automation", null as "not stated".
  - `intigritiMe` true for 109/146: test accounts must use your `@intigriti.me` address.
  - `requestHeader` set for 29, always `X-Intigriti-Username: {Username}` (spacing varies).
  - `userAgent` set for 24.
  - `safeHarbour` false for 10.

## Not available

- Bounty amounts per tier or severity (only min/max per program). Tables, if any, are on the web page.
- Any competition data: submissions, researchers, accepted reports, response times.
- Program launch date. A lower bound comes from the earliest activity we see for it.

## Consequences (applied in CONTRACTS §2–§4)

- The guard's rate limit = `automatedTooling` (0 → manual only, null → 2 req/s default).
- Payout = min/max (currency-converted) × share of in-scope assets in paid tiers.
- Competition is a proxy: confidentiality, needs-join, program age (from activities), bounty level.
- Programs that 403 are listed as **join to see scope**: ranked on list fields only and marked.
