# JTR Travel: an accessible, DTS-style travel management prototype

A working prototype of a DoD travel-management web application that follows the **Joint Travel Regulations (JTR)**.
It is built for **Section 508 / WCAG 2.2 AA** and has a **fully editable RBAC** permission system.

> Prototype: not an official government system. Per diem rates are sample data. Do not enter real PII or CUI.

## Run it

```bash
cd ~/jtr-travel
./run.sh                # creates the venv if needed, starts http://127.0.0.1:5000
```

Reset the database to demo data at any time:

```bash
.venv/bin/flask --app app init-db
```

Demo accounts (password `Password123!`): `traveler`, `traveler2`, `preparer`, `budget`, `ao`, `co`, `dta`,
`auditor`, `admin`.

**Try the full flow:** sign in as `traveler`, create an authorization, add San Diego, add airfare, and sign. Then
sign in as `budget` and stamp it under Approvals, then as `ao` and approve it. Back as `traveler`, choose
*Create voucher* on Home, enter the actual lodging cost, and sign. Then sign in as `ao` and approve, and as `co`
and certify.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests cover the JTR per diem engine, RBAC scoping, expiry, escalation and delegation, policy import/export, the
end-to-end routing lifecycle, separation of duties, and an accessibility lint of every page.

## Feature coverage vs. DTS

| DTS capability | Status |
|---|---|
| Traveler profile (personal, GTCC, EFT, default routing) | ✅ |
| Authorizations (TDY orders) with itinerary, per diem and expenses | ✅ |
| Vouchers created from approved authorizations | ✅ |
| Local vouchers | ✅ |
| Amendments | ✅ |
| JTR per diem: locality and seasonal rates, 75% first/last day, lodging cap, gov quarters, long-term TDY flat rate | ✅ |
| POV mileage, receipts ≥ $75, GTCC mandatory use, rental car justification, laundry rule | ✅ pre-audit flags |
| Routing lists with stamps: review, funds, approve, certify, return, override, cancel | ✅ fully configurable |
| Lines of accounting and funds-available tracking | ✅ |
| Permission levels / org access / NDEA | ✅ replaced by RBAC (see [docs/RBAC.md](docs/RBAC.md)) |
| Reports + CSV | ✅ status, traveler, funds, aging |
| Audit log | ✅ |
| CAC/PIV login | 🔜 SAML/OIDC integration point documented |
| Reservations (CTO/GDS flights, hotels, rental cars) | 🔜 needs a commercial travel office integration |
| Receipt uploads | 🔜 |
| Group authorizations, PCS, OCONUS, conference/LTDY special cases, advances, debt collection, DFAS payment interface | 🔜 |

## Project layout

```
app/
  rbac.py          permission catalog, default roles, evaluation, SoD, policy import/export
  jtr.py           per diem engine and pre-audit rules
  workflow.py      routing / stamping state machine
  schema.sql       database schema
  seed.py          demo orgs, users, rates, LOAs, routing list
  views/           Flask blueprints (auth, main, documents, admin, reports)
  templates/       Jinja templates (_macros.html holds the accessible form components)
  static/          CSS (contrast-checked palette) and progressive-enhancement JS
docs/RBAC.md          permission system map
docs/ACCESSIBILITY.md 508 techniques and manual test plan
tests/
```

## Before production

* Replace SQLite with PostgreSQL and add migrations (Alembic).
* Use CAC/PIV authentication and remove local passwords.
* Pursue an ATO: NIST 800-53 controls and DoD IL4/IL5 hosting (for example a FedRAMP High / DoD-authorized cloud).
* Load official GSA/DTMO per diem tables and have a JTR SME validate the rules engine.
* Complete a Trusted Tester 508 evaluation and publish an ACR/VPAT.
