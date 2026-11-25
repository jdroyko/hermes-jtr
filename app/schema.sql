PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Organizations (hierarchical). Role assignments are scoped to an org and,
-- optionally, all of its descendants.
-- ---------------------------------------------------------------------------
CREATE TABLE orgs (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES orgs(id)
);

CREATE TABLE users (
    id                       INTEGER PRIMARY KEY,
    username                 TEXT NOT NULL UNIQUE,
    password_hash            TEXT NOT NULL,
    first_name               TEXT NOT NULL,
    last_name                TEXT NOT NULL,
    email                    TEXT NOT NULL,
    phone                    TEXT,
    org_id                   INTEGER NOT NULL REFERENCES orgs(id),
    rank_grade               TEXT,
    duty_station             TEXT,
    residence_city           TEXT,
    gtcc_last4               TEXT,
    eft_last4                TEXT,
    default_routing_list_id  INTEGER REFERENCES routing_lists(id),
    active                   INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- RBAC
-- permissions : catalog of atomic capabilities (keys are checked in code)
-- roles       : named, editable bundles of permissions
-- role_assignments : user + role + scope (org / subtree / global) + expiry
-- sod_rules   : toggleable separation-of-duties constraints
-- ---------------------------------------------------------------------------
CREATE TABLE permissions (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE roles (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    legacy_level TEXT,             -- maps to DTS permission level(s), informational
    is_system    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE role_permissions (
    role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- Delegated administration: holders of role_id (with admin.assign in scope)
-- may grant grantable_role_id even if they do not hold its permissions.
CREATE TABLE role_grantable (
    role_id           INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    grantable_role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, grantable_role_id)
);

CREATE TABLE role_assignments (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id          INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    org_id           INTEGER REFERENCES orgs(id),   -- NULL = global
    include_children INTEGER NOT NULL DEFAULT 1,
    expires_at       TEXT,                          -- ISO date, NULL = never
    granted_by       INTEGER REFERENCES users(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sod_rules (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------------
-- Routing lists: ordered steps; each step requires a permission (and may be
-- pinned to a specific user). Fully editable by routing officials.
-- ---------------------------------------------------------------------------
CREATE TABLE routing_lists (
    id          INTEGER PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE routing_steps (
    id                  INTEGER PRIMARY KEY,
    routing_list_id     INTEGER NOT NULL REFERENCES routing_lists(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    name                TEXT NOT NULL,
    stamp               TEXT NOT NULL,           -- e.g. REVIEWED, APPROVED, CERTIFIED
    required_permission TEXT NOT NULL,           -- permission key
    assignee_user_id    INTEGER REFERENCES users(id),
    doc_types           TEXT NOT NULL DEFAULT 'AUTH,VOUCHER,LOCAL'
);

-- ---------------------------------------------------------------------------
-- Finance
-- ---------------------------------------------------------------------------
CREATE TABLE loas (
    id            INTEGER PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES orgs(id),
    label         TEXT NOT NULL,
    code          TEXT NOT NULL,
    fiscal_year   INTEGER NOT NULL,
    budget_amount REAL NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE per_diem_rates (
    id           INTEGER PRIMARY KEY,
    location     TEXT NOT NULL,
    state        TEXT NOT NULL,
    season_start TEXT NOT NULL DEFAULT '01-01',  -- MM-DD
    season_end   TEXT NOT NULL DEFAULT '12-31',  -- MM-DD
    lodging      REAL NOT NULL,
    mie          REAL NOT NULL,
    fiscal_year  INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending')),
    requested_by INTEGER REFERENCES users(id),   -- traveler who typed a location not on the list
    document_id  INTEGER REFERENCES documents(id),  -- the document that first proposed it
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- Travel documents
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id                INTEGER PRIMARY KEY,
    doc_number        TEXT NOT NULL UNIQUE,
    doc_type          TEXT NOT NULL CHECK (doc_type IN ('AUTH','VOUCHER','LOCAL')),
    traveler_id       INTEGER NOT NULL REFERENCES users(id),
    created_by        INTEGER NOT NULL REFERENCES users(id),
    org_id            INTEGER NOT NULL REFERENCES orgs(id),
    status            TEXT NOT NULL DEFAULT 'CREATED',
    trip_type         TEXT NOT NULL DEFAULT 'TDY',
    trip_purpose      TEXT NOT NULL DEFAULT '',
    trip_description  TEXT NOT NULL DEFAULT '',
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    loa_id            INTEGER REFERENCES loas(id),
    routing_list_id   INTEGER REFERENCES routing_lists(id),
    current_step_seq  INTEGER,
    parent_auth_id    INTEGER REFERENCES documents(id),
    amendment         INTEGER NOT NULL DEFAULT 0,
    justification     TEXT NOT NULL DEFAULT '',
    total_estimate    REAL NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE document_stops (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    rate_id         INTEGER NOT NULL REFERENCES per_diem_rates(id),
    arrive_date     TEXT NOT NULL,
    depart_date     TEXT NOT NULL,
    lodging_nightly REAL,          -- actual nightly cost (voucher); NULL = use max
    gov_quarters    INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE expenses (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    expense_date     TEXT NOT NULL,
    category         TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    amount           REAL NOT NULL DEFAULT 0,
    miles            REAL,
    payment_method   TEXT NOT NULL DEFAULT 'GTCC',
    receipt_attached INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE attachments (
    id           INTEGER PRIMARY KEY,
    expense_id   INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,          -- original name, display only
    stored_name  TEXT NOT NULL UNIQUE,   -- on-disk name under instance/uploads/<document_id>/
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    uploaded_by  INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE document_history (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    stamp       TEXT,
    remarks     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    details    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_assign_user ON role_assignments(user_id);
CREATE INDEX idx_docs_traveler ON documents(traveler_id);
CREATE INDEX idx_docs_org ON documents(org_id);
CREATE INDEX idx_hist_doc ON document_history(document_id);
CREATE INDEX idx_attach_expense ON attachments(expense_id);
CREATE INDEX idx_attach_doc ON attachments(document_id);
