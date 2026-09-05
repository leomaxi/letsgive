# Let's Give

Live contribution-participation platform for religious organizations and nonprofits. Full product
requirements and technical specification: see the original PRD document.

## Development roadmap

1. **Foundation & Core Infrastructure** — multi-tenant schema, RBAC, MFA, audit log.
2. **Session Workflow & OTP Approval** — session state machine, finance approval codes, simulator.
3. **Live Mailbox Ingestion** — mailbox connections, parsers, webhook ingestion, dedup, ledger.
4. **Projection & Display Studio** — canvas/templates, realtime WebSocket, OBS output.
5. **Reconciliation & SaaS Operations** — reconciliation queue, reports/exports, billing, quotas.
6. **Hardening & Pilot Launch** *(current)* — security review, backup/restore drills, accessibility.

All six backend phases are implemented at MVP depth. See "Known gaps" at the end of each phase's
section below for what a real production launch still needs — mainly real OAuth/payment provider
credentials this environment doesn't have. A `frontend/` now exists too (see below) and is being
built out incrementally on top of the finished API.

## Backend

```
backend/
  app/
    core/        settings, password hashing, JWT (access + scoped display tokens), TOTP MFA
    db/          SQLAlchemy async engine + models (orgs, users, memberships, sessions, approvals,
                 mailbox_connections, parser_profiles, contribution_events, display_templates,
                 display_elements, reconciliation_items, plans, audit_logs)
    domain/      RBAC, audit recording, session state machine, OTP notifier, mailbox provider
                 abstraction, parser, ingestion pipeline, realtime pub/sub broadcaster, billing
                 entitlement checks
    api/v1/      FastAPI routers (auth, organizations, sessions, connections, display-templates,
                 reconciliation, reports, plans, audit, webhooks) and schemas
    api/         display_page.py -- the browser-facing (non-JSON, non-versioned) fullscreen/OBS page
    workers/     placeholder for a real async task queue (Celery/Dramatiq/ARQ) once ingestion
                 needs to run off the request path instead of inline in the webhook handler
    integrations/ placeholder for real Microsoft Graph / Gmail SDK calls, and for a real
                 payment provider (Stripe et al.) once billing needs actual invoicing
  migrations/    Alembic migrations (0001 tenant/auth, 0002 sessions/approvals, 0003 mailbox
                 ingestion, 0004 display studio, 0005 reconciliation/billing)
  scripts/       backup_restore.py -- a real, working SQLite backup/restore (spec 12 "Recovery");
                 documents the equivalent pg_dump/pg_restore commands for production Postgres
  tests/         pytest + httpx async test suite (in-memory SQLite); test_realtime.py uses a
                 sync starlette.testclient.TestClient instead, the only way to test WebSocket routes
```

### Setup

```bash
cd backend
python -m venv ../.venv   # or reuse an existing venv
../.venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env      # then set LETSGIVE_DATABASE_URL to your Postgres or MySQL instance
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```

Without a `.env`, the app defaults to a local SQLite file for zero-setup development; set
`LETSGIVE_DATABASE_URL` to a `postgresql+asyncpg://` or `mysql+aiomysql://` URL for anything beyond
local exploration. Both are real, supported backends (not just Postgres) — every model column is
explicitly lengthed for MySQL's stricter VARCHAR rules, and every migration is dialect-aware (see
migration `0008`'s MySQL/Postgres branches for its one genuinely dialect-specific step, adding new
enum labels).

### Tests

```bash
cd backend
python -m pytest
```

### What Phase 1 implements

- Tenant model (`organizations`), users, `memberships` (role + status), append-only `audit_logs`.
- JWT auth (register/login), TOTP MFA enrollment/activation.
- RBAC: owner-only member invitation; **Owner/Finance roles require MFA already enabled** on the
  invitee (spec 3.1), enforced both at org-creation (self) and at invite time.
- Tenant isolation: cross-tenant reads return 404 (not 403), avoiding org-existence leaks;
  `organization_id` is enforced via a per-request `membership` dependency, not ad hoc checks.
- Immutable audit events for organization creation and member invitations.

### What Phase 2 adds

- `sessions` (state machine: draft → approval_requested → authorized → live ⇄ paused → ended,
  plus reconciling/closed reserved for Phase 5) and `approvals` (finance OTP codes) models.
- End-to-end OTP approval flow (spec §4): Media requests approval → 6-digit code (Argon2-hashed,
  10-minute TTL, 5-attempt lockout) delivered to every active Finance officer through a swappable
  `Notifier` abstraction (dev default just logs it — see `app/domain/notifications.py` — Phase 5
  swaps in real email/SMS) → Media submits the code finance read out to them → session becomes
  `authorized` and the mailbox watermark is set to that instant.
- Session controls: start/pause/resume (pausing freezes the countdown and resuming shifts `ends_at`
  forward by the paused duration, not just flips a flag), extend, close — all guarded by
  **optimistic-lock versioning** (`expected_version` in every mutating request; stale version → 409).
- Finance-only `amount_visible` toggle while a session is live, audited before/after (acceptance
  criterion in spec §15).
- Short-lived, session-scoped **display tokens** for the public projection endpoint
  (`GET /v1/sessions/{id}/public`) — distinct JWT `typ` claim keeps them from working as normal user
  bearer tokens, and they carry no finance permissions (spec §11.1). The public payload is sanitized
  by construction: the response schema simply has no field capable of carrying donor identity or
  raw email content.
- Contribution count/amount on the public and operator views are placeholders (`0` / `null`) until
  Phase 3 wires up the real ledger.

### What Phase 3 adds

- `mailbox_connections` (dedicated mailbox + provider + status + per-connection webhook secret),
  `parser_profiles` (sender allowlist, credit/reject keyword lists, amount-extraction regex,
  confidence threshold, versioned via `template_version`), and `contribution_events` — the
  append-only ledger (spec §10 "Core Data Model" / §9.2 "ledger"). Deliberately has **no** column
  for sender, subject or body: spec §8 requires minimizing retained email content, so donor
  identity can't leak through this table even by a future bug — the field doesn't exist to leak.
- A `MailboxProvider` protocol (`app/domain/mailbox_providers.py`) abstracts OAuth + webhook-signature
  validation + message fetch. `FakeMailboxProvider` is a full working dev/test implementation (no
  network, no credentials); `MicrosoftGraphProvider`/`GmailProvider` are wired into every endpoint
  but raise `NotImplementedError` — real Graph/Gmail calls need a registered OAuth app and live
  credentials this environment doesn't have. Swapping them in touches this one file, same pattern
  as `Notifier` in Phase 2.
- End-to-end ingestion pipeline (spec §9.1): webhook → HMAC-SHA256 signature check → message fetch
  → session matching by watermark/time-window → parser-profile matching → decision. Every message
  gets exactly one `ContributionDecision`: `accepted`, `ambiguous` (matched + credit intent, but
  confidence below the profile's threshold — counted nowhere, held for a future reconciliation
  queue), or one of three `excluded_*` reasons (time window, sender mismatch, no credit intent) —
  spec §5.3's validation table, made explicit and queryable rather than a single boolean.
- Idempotent by construction: a unique constraint on `(organization_id, mailbox_connection_id,
  provider_message_id)` means redelivering the same provider webhook is a safe no-op, not a
  double-count — checked before insert, and the insert's own constraint is the backstop if two
  deliveries race each other.
- Session/public/operator totals (`contribution_count`, `total_amount`) are now **live aggregates**
  over `accepted` ledger rows — never a separately-mutated counter, so there's nothing to
  double-increment (spec §10.1).
- `POST /v1/sessions/{id}/simulate-deposit` — the "test mode" feature spec §5.4 calls for: lets
  Media/Finance rehearse the display with synthetic deposits on a `test_mode` session, going
  through the same ledger and totals as a real event, clearly tagged `test_mode=true`.

### What Phase 4 adds

- `display_templates` (16:9 canvas config + name + `is_default`) and `display_elements` (position,
  size, z-index, style, binding, lock/hide) — spec §6.1's drag/drop canvas data model. A session
  optionally binds to a template (`display_template_id`); creating a session with none specified
  auto-binds the organization's `is_default` template if it has exactly one, matching spec §6.2's
  "under 60 seconds" activation goal. `PUT .../display-templates/{id}` is a full-document
  save (whole canvas + element list each time) rather than granular per-element endpoints — the
  natural shape for a frontend's autosave, and simpler than reconciling piecemeal edits without an
  actual drag/drop UI in this repo to drive it.
- **Realtime pub/sub** (`app/domain/realtime.py`): an in-process `SessionBroadcaster` that every
  session-state-changing endpoint (start/pause/resume/extend/close/visibility/verify, an accepted
  webhook ingestion, simulate-deposit) publishes to after commit. Explicitly process-local for now
  — spec §9 recommends Redis pub/sub for a horizontally-scaled deployment; swapping this module's
  internals for a Redis-backed version is the only change needed; every caller already only sees
  queue-shaped `subscribe`/`publish` semantics.
- `WS /v1/sessions/{id}/live` — the public projection pushes sanitized state instead of the client
  polling `GET .../public`. Auth is a display token in the query string (browsers can't attach
  custom headers to a WebSocket handshake); the handler explicitly closes its DB session right
  after the initial fetch rather than holding a connection open for the socket's whole lifetime.
- `GET /display/{session_id}?token=...` — a real, working fullscreen page (spec §6.2 "Fullscreen
  output") suitable for a projector window or an OBS Browser Source: connects to the WS channel,
  renders org name / large contribution count / amount (if visible) / countdown, auto-reconnects
  with backoff. **Verified live in an actual browser** for this phase (not just automated tests):
  watched the count and amount update in place via two `simulate-deposit` calls with zero page
  reload. This is a minimal server-rendered page, not the rich drag/drop Studio editor itself —
  see gaps below.

### What Phase 5 adds

- `reconciliation_items` — the finance review queue (spec §7). Auto-created only for genuinely
  ambiguous outcomes: `AMBIGUOUS` (parser matched but confidence too low) and
  `EXCLUDED_TIME_WINDOW` (arrived just before the session's watermark — might belong to it
  anyway). Confidently-rejected events (wrong sender, no credit intent, no active session at all)
  are never queued; they were never ambiguous, so reviewing them would just be noise.
  `EXCLUDED_TIME_WINDOW` events are now attributed to the nearest session for this reason even
  though they don't count toward its total — previously (Phase 3) they were orphaned with no
  `session_id` at all.
- `POST /v1/reconciliation/{id}/resolve` (Finance) — **never mutates the original ledger event.**
  Resolving `excluded` just closes the queue item. Resolving `accepted` appends a *new* immutable
  `ContributionEvent` (`corrects_event_id` pointing at the original) that counts toward the
  session's totals — spec §7's "Corrections append events and preserve original evidence; records
  are never silently overwritten," enforced structurally rather than by convention. **Verified
  live**: seeded an ambiguous event, resolved it via real HTTP, confirmed the original event was
  still `ambiguous`/unchanged and a linked `accepted` correction had appeared in operator totals.
- `GET /v1/reports/sessions/{id}` (Finance/Auditor/Owner) — validated count/amount, an
  excluded-reason breakdown, resolved corrections, full approval history, and the bound mailbox
  connection's health snapshot (spec §7 "Session summary"). `GET .../export.csv` — the same
  ledger as a downloadable CSV (spec §7 "integration-ready output for accounting").
- `GET /v1/organizations/{id}/audit-logs` — the first *read* path for `AuditLog`, which Phase 1
  could only write to. Owner/Finance/Auditor only; this is exactly the read-only oversight the
  Auditor role exists for.
- `plans` (seeded via migration: Starter/Growth/Premium/Enterprise, spec §13's illustrative tiers)
  and `Organization.subscription_status`/`grace_period_ends_at`. Entitlements are **actually
  enforced server-side**, not just displayed: session creation checks the plan's monthly quota,
  connection creation checks its mailbox-connection cap — both verified live via real 402
  responses once a Starter org's limits were hit. `POST .../subscription/cancel` stops new
  sessions/connections/templates immediately; historical reports stay readable through
  `grace_period_ends_at`, and — once it actually passes — are cut off too (`assert_reports_readable`
  in `app/domain/billing.py`; see the Phase 6 hardening entry below for the full writeup).

### What Phase 6 adds

- **Login rate limiting** (`app/domain/rate_limit.py`) — spec §8 lists "rate limiting" as a required
  Authentication control, and until now `/v1/auth/login` had none: unlimited password/MFA-code
  guessing was possible. Now a fixed-window limiter (10 attempts / 15 min, keyed by email) returns
  `429` past the threshold and resets on a successful login. **Verified live**: 11 consecutive wrong
  passwords against a real account got a `429` on the 11th, and the *correct* password was rejected
  too while the window was still open — the point being an attacker who eventually guesses right
  doesn't win.
- **The fail-safe operator warning** spec §4 calls for ("If the email provider disconnects,
  notifications are delayed, or parsing confidence drops... show a private operator warning") was
  flagged back in Phase 2 as an unset column and never actually implemented until now.
  `compute_operator_warning()` (`app/domain/sessions.py`) is computed fresh on every operator read
  (never persisted, so it can't go stale) from three real signals: the bound mailbox connection's
  status/staleness, and the count of pending reconciliation items. **Verified live**: revoking a
  session's mailbox connection produced `"Mailbox connection is 'revoked'..."` on the next operator
  poll while a webhook against that connection was silently `ignored`, and the warning never
  appeared on the public payload (the field doesn't exist in `SessionPublicOut`).
- **A real backup/restore drill**, not just documentation: `scripts/backup_restore.py backup`
  wraps SQLite's own online backup API (transactionally consistent even mid-write, not a raw file
  copy). Actually ran it end to end — backed up the dev DB, deleted the original outright, restored
  from the backup, and confirmed both the row counts and a real user record (password hash + MFA
  secret intact) survived by starting the server against the restored file and logging in. Postgres
  production backups are `pg_dump`/`pg_restore` (documented in the same script via
  `postgres-commands`) — not exercised here since there's no live Postgres instance in this
  environment, unlike SQLite.
- **Accessibility pass on `GET /display/{id}`** (spec §6.3, WCAG 2.2 AA) — the one real HTML page in
  this repo. Added semantic landmarks (`<main>`, `<h1>` for the org name, `role="status"` for the
  connectivity indicator, `role="timer"` for the countdown), an `aria-live="polite"` region around
  the count/amount specifically (*not* the once-per-250ms-ticking timer, which would spam a screen
  reader with updates every quarter-second), and screen-reader-only context text. Verified via the
  browser's accessibility tree, not just visually. `prefers-reduced-motion` support and high-contrast
  dark-on-near-white color choices already existed from Phase 4.
- **Closed a real test-coverage gap** found while checking spec §15's acceptance criteria one by
  one: "Mailbox revocation prevents further retrieval immediately and surfaces a private health
  alert" had zero test coverage despite being implemented. Added
  `test_revoked_connection_stops_ingestion_and_warns_operator`.
- All ten of spec §15's acceptance criteria now trace to passing, named tests — see the checklist
  below.

### Acceptance criteria (spec §15)

| # | Criterion | Where it's proven |
|---|---|---|
| 1 | Media can't start monitoring without a valid, unexpired finance code | `test_sessions.py`, `test_wrong_code_locks_after_max_attempts`, `test_expired_code_rejected` |
| 2 | Session ignores messages before its watermark / after closure | `test_ingestion.py::test_message_before_watermark_excluded_as_time_window` |
| 3 | Re-delivery of the same provider message never double-counts | `test_ingestion.py::test_duplicate_webhook_delivery_is_idempotent` |
| 4 | Ambiguous/invalid notifications go to reconciliation, don't affect the public display | `test_ingestion.py::test_low_confidence_is_ambiguous_and_not_counted`, `test_reconciliation.py` |
| 5 | Public API/output carry no donor identity, raw content, or hidden amount | structural: `SessionPublicOut`/`ContributionEvent` have no such fields at all |
| 6 | Finance can toggle amount visibility live, with an audit entry | `test_sessions.py::test_full_session_lifecycle`, `test_audit.py` |
| 7 | Timer and count update on connected clients without a full refresh | `test_realtime.py`; verified live in an actual browser (Phase 4) |
| 8 | Mailbox revocation stops retrieval immediately and warns the operator | `test_hardening.py::test_revoked_connection_stops_ingestion_and_warns_operator` |
| 9 | Corrections preserve the original event with an attributable audit trail | `test_reconciliation.py::test_ambiguous_event_appears_in_queue_and_can_be_accepted` |
| 10 | No role can access another organization's records | tenant-isolation tests across every phase (`test_tenant_isolation.py` and others) |

### Known gaps / what a production launch still needs

- *(Historical note, resolved)* This paragraph originally said `mfa_secret` and refresh tokens
  weren't envelope-encrypted. `mfa_secret` now is (`app/domain/crypto.py`'s `EncryptedString`, a
  Fernet-backed `TypeDecorator`) — see the entry near the end of this section for the full writeup.
  Refresh tokens don't exist as a concept in this app (JWT access tokens only, no refresh flow), so
  there was never anything there to encrypt.
- No OIDC/SSO delegation yet; Phase 1 uses local password + TOTP so the tenant/RBAC foundation is
  testable without an external identity provider dependency.
- *(Historical note, partially resolved)* This paragraph originally said `Notifier` was a logging
  stub only, not a real provider. The email half is now real: `SmtpNotifier`
  (`app/domain/notifications.py`) sends the finance-approval OTP over actual SMTP (stdlib
  `smtplib`, run off the event loop via `asyncio.to_thread` since it's a low-volume,
  latency-insensitive send, not a hot path — not worth an async SMTP dependency). Selected
  automatically the moment `LETSGIVE_SMTP_HOST` is set (new `Settings.smtp_host` and friends in
  `app/core/config.py`); an unset host (the default) keeps the existing `LoggingNotifier` behavior,
  so a fresh dev checkout needs zero configuration, same "dev-friendly default, override in
  production" convention as `jwt_secret`/`encryption_key`. SMS remains out of scope — that needs a
  paid provider (e.g. Twilio) and live credentials this environment doesn't have, unlike SMTP, which
  needs only a hostname the operator supplies at deploy time; nothing about writing or testing the
  SMTP path itself required real credentials. New tests (`tests/test_notifications.py`): two mock
  `smtplib.SMTP` to verify `SmtpNotifier` builds the right message and makes the right protocol calls
  (including that STARTTLS/login are correctly skipped when not configured), two verify
  `_build_default_notifier()` picks the right implementation based on settings. Regression-tested the
  same disciplined way as everything else in this project: temporarily made STARTTLS/login
  unconditional, confirmed the "skips them when unconfigured" test actually went red, then restored
  the guard and reconfirmed all 94 backend tests green. **Verified live with a real SMTP protocol
  round-trip, not just mocks**: wrote a throwaway ~40-line asyncio TCP server speaking just enough
  SMTP (EHLO/MAIL FROM/RCPT TO/DATA) to accept a real message, pointed a real running instance of
  this app at it via `LETSGIVE_SMTP_HOST`, and drove an actual finance-approval-request flow
  end-to-end through the real API (register two users, enroll MFA, create an org, invite and accept
  a Finance member, create and request approval on a real session) — the stub genuinely received a
  full SMTP transaction over a real TCP connection, with the correct To/From/Subject and the
  session's actual OTP code and session ID in the body, matching a session sitting in
  `approval_requested` waiting for exactly that code.
- SQLite (the zero-setup dev default) silently drops timezone info from `DateTime(timezone=True)`
  columns on read, unlike Postgres. **Fixed at the type level** while building the frontend's Session
  console (see the Frontend section): every model now uses `app/db/base.py`'s `UTCDateTime`
  TypeDecorator instead of raw `DateTime(timezone=True)`, which normalizes to aware UTC in
  `process_result_value` regardless of dialect — so every timestamp serializes with a proper UTC
  offset in every API response now, not just where `ensure_utc()` had been manually applied to a
  Python-side comparison. `ensure_utc()` itself is still there and still used in a few business-logic
  comparisons as defense in depth, but the systemic serialization gap it didn't cover is closed. A
  related SQLite gap remains: it can't `ALTER TABLE ADD COLUMN`/`DROP COLUMN` with an inline FK
  constraint outside of Alembic's batch mode (see migration `0003`'s `op.batch_alter_table`) — Postgres
  wouldn't have needed it,
  but the dev DB does.
- Real Microsoft Graph / Gmail OAuth and message-fetch calls aren't implemented (see above) — only
  the `fake` provider works today. `token_ref` is still a placeholder column rather than a real
  pointer into a secret store, since there's no real OAuth token to reference yet — see the envelope
  encryption entry near the end of this section for what *is* encrypted today.
- The webhook handler runs signature validation, message fetch, and parsing **inline** in the
  request/response cycle rather than acking immediately and handing off to a queue. Spec §9
  calls for Celery/Dramatiq/ARQ workers with retries and a dead-letter queue; that infra (and the
  `workers/` package) is still a placeholder — fine for a single fake/dev message, not for
  production webhook volume or provider retry semantics.
- *(Historical note, resolved)* This paragraph originally said a parser profile match picked the
  *first* active profile whose sender pattern matched, in whatever order the database happened to
  return them — an org with genuinely overlapping sender patterns across profiles (one for
  `@bank.com` broadly, another for the specific `deposits@bank.com`) could see ordering-dependent
  behavior, not because it made sense, just because of iteration order. Now resolved two ways:
  `app/domain/parsing.py`'s `parse_message` reports a `match_specificity` (0 = no match, 1 = a
  `@domain` wildcard, 2 = an exact address) alongside its existing result, and
  `app/domain/ingestion.py`'s selection now picks whichever matching profile is *most specific*,
  not whichever came first — an exact-address profile beats a domain-wildcard one regardless of
  which was created first or listed first by the query. Remaining ties (e.g. two profiles with the
  literal same pattern) fall back to a newly-added `.order_by(ParserProfile.created_at)` on the
  query, so behavior is fully deterministic even in that edge case, not just "usually consistent."
  5 new tests: 4 unit tests (`tests/test_parsing.py`) on the specificity scoring itself (no match,
  domain vs. exact, specificity populated on every return path including the reject/no-credit-intent
  short-circuits, and a profile with multiple patterns taking the most specific one that matches
  *within itself*), plus a real end-to-end integration test
  (`test_more_specific_parser_profile_wins_over_a_broader_overlapping_one`) that creates the broad
  profile *first* and the exact one *second* specifically so creation order alone couldn't
  accidentally produce the right answer, delivers a real signed webhook, and asserts the resulting
  ledger event's `template_version` came from the exact profile. Regression-tested the same
  disciplined way as everything else in this project: reverted the specificity-based selection back
  to the old first-match loop, confirmed the integration test failed with a real, readable
  `'v1-broad' == 'v2-exact'` diff (not a vacuous pass — an earlier draft of this test that reused an
  existing helper's already-created profile order passed even against the broken code, which is why
  it was rewritten to control creation order explicitly), then restored the fix and reconfirmed all
  99 backend tests green.
- *(Historical note, resolved — see the Frontend section below)* This paragraph originally said there
  was no drag/drop Display Studio editor and that `GET /display/{id}` ignored a template's elements
  entirely, back when this repo was backend-only. Both are now built: a full drag/resize editor lives
  at `/display-studio` in the frontend, and the public display page renders a session's bound template
  when one exists (falling back to the original fixed layout when it doesn't).
- The realtime broadcaster is in-process only (see above) — fine for one server process, silently
  drops updates for clients connected to a different process/instance. Not an issue until this
  needs to run behind a load balancer with multiple workers.
- *(Historical note, resolved — see the Frontend section below)* This paragraph originally said
  there was no operator-facing realtime channel and Media/Finance polled `GET .../operator`. There's
  now a second WebSocket channel (`/{id}/live-operator`) purpose-built for the operator console,
  alongside the existing public one.
- `SessionPublicOut`'s `currency` field is always the session's configured currency, independent of
  whether an amount is actually visible — harmless (currency alone isn't sensitive), but worth
  knowing if a future field audit assumes every field toggles with `amount_visible`.
- No real payment provider — "billing" here is plans, entitlement enforcement, and cancellation,
  not actual invoicing or payment collection (spec §13's "invoices" line item). Needs a Stripe-like
  integration with live credentials this environment doesn't have, following the same
  provider-behind-a-protocol pattern as `Notifier` and `MailboxProvider`.
- *(Historical note, resolved)* This paragraph originally said `grace_period_ends_at` was recorded
  on cancellation but not enforced, so reports stayed readable indefinitely. Now enforced:
  `assert_reports_readable` (`app/domain/billing.py`) blocks both the JSON report and the CSV export
  with a `402` once `grace_period_ends_at` has actually passed — a org still within its 30-day grace
  period sees no change. Deliberately scoped to just those two report endpoints, not the live
  operator console (`GET .../operator`, `GET .../events`): those aren't "historical reports," they're
  how an Owner/Auditor checks in on a session, canceled subscription or not, and the spec's grace
  period language is specifically about report access. The frontend's `SessionReportPage` and its CSV
  download button were both showing a generic "Could not load"/"Could not download" message for any
  failure, encryption included, before this — now they surface the actual backend detail (matching
  every other page in this app's error-handling convention), so a canceled org's Owner sees the real
  "contact support to reactivate" message instead of a dead end. Verified live end-to-end: canceled a
  real subscription, pushed `grace_period_ends_at` into the past via a direct DB write (nothing
  reachable through the API advances real time that far), and confirmed the report page showed the
  specific backend message while the operator console for the same session stayed fully reachable.
  Caught the same way as the other regression tests this session: reverted the fix, confirmed
  `test_reports_stay_readable_within_grace_period_but_not_after` actually went red, then restored it.
- *(Historical note, resolved)* This paragraph originally said PDF export wasn't implemented, only
  CSV. It now is: `GET /v1/reports/sessions/{id}/export.pdf` (`app/domain/pdf_report.py`, built with
  reportlab's `platypus` layer — flowables and tables, not hand-placed canvas coordinates, so it
  paginates itself if a session ever has enough corrections/approvals to need it). Same role gating
  and grace-period cutoff as the JSON report and CSV export it sits beside; same underlying data
  (`_build_report`) as both, just laid out for printing or attaching to board minutes, where the CSV
  covers the raw per-event ledger for accounting/integration. **Found and fixed a real rendering bug
  while writing the test for this**: the placeholder "—" (em dash) used for empty fields didn't
  survive the PDF's font encoding, extracting back as a replacement character (`�`) — not a problem
  CSV or JSON export have, since they're not glyph-rendered. Switched to a plain ASCII "-" throughout.
  This was only caught because the test does real content verification, not just a status-code and
  magic-bytes check: it parses the PDF back with `pypdf` and asserts the session's actual org name,
  session ID, validated amount, and connection status appear in the extracted text — a raw
  byte/substring search over the PDF bytes would never find this text even in a correctly-rendered
  document, since content streams are FlateDecode-compressed by default. Verified the test actually
  catches regressions the same disciplined way as everything else this session: reverted the org-name
  argument to an empty string, confirmed the test went red with a real, readable diff, then restored
  it. Verified live in the browser too: downloaded a real session's PDF via the report page's new
  button, confirmed a `200` with the right content type.
- *(Historical note, resolved)* This paragraph originally said there was no per-plan quota on display
  templates or team-member seats, only sessions/month and mailbox connections. Both are now enforced
  the same way, in `app/domain/billing.py`: `assert_can_create_display_template` (wired into create
  *and* duplicate — duplicating is still creating a new row) and `assert_can_add_team_member` (wired
  into the invite endpoint; a **pending** invitation reserves a seat immediately, the same as an
  active member, and declining frees it back up — verified by a dedicated test, not just asserted in
  a comment). Seeded tiers: Starter 2 templates / 5 seats, Growth 5/15, Premium 20/50, Enterprise
  100/500 (migration `0007`, verified both directions against a real SQLite file). `PlanOut` and the
  frontend's `Plan` type both got the two new fields; the Billing page's plan cards show them
  automatically since they already iterate the plan's fields generically. Regression-tested the same
  disciplined way as everything else this session: reverted each of the two enforcement call sites in
  turn, confirmed the matching test actually went red, then restored them. Verified live: exhausted a
  real Starter org's 2-template quota via the API, then confirmed Display Studio's "New template"
  button surfaced the exact backend message ("Display template limit reached for the 'Starter' plan
  (2). Upgrade or delete an existing template.") rather than a generic failure — the specific-message
  wiring for both new checks was already in place from earlier billing/template-page work, so no
  frontend error-handling changes were needed, just the two new schema fields.
- *(Historical note, resolved)* This paragraph originally said reconciliation only ever produced a
  counting correction (accept-with-amount or exclude), and that spec §7's "reversed" and "duplicate"
  review categories weren't modeled — no decision existed for "this un-does an earlier accepted
  event." Both now exist as `ReconciliationResolution` values, resolved through the same review
  queue as accept/exclude:
  - **`reversed`** — for when the item's *own* underlying event (an ambiguous/borderline
    notification, same auto-creation path as always) turns out to be a notice that an *earlier*
    ACCEPTED deposit was returned. Resolving requires `reverses_event_id` (which prior accepted
    event this un-does); the backend validates it's a real ACCEPTED event in the same session and
    hasn't already been reversed (`409` if it has). Never mutates the event being reversed — appends
    a new immutable `REVERSED` ledger row (`corrects_event_id` pointing at it), the same
    append-only pattern `accepted` corrections already used. `ContributionDecision` gained a
    matching `REVERSED` value.
  - **`duplicate`** — ledger-wise identical to `excluded` (nothing new created, the original event
    untouched), but tracked as its own value so the reviewer's actual reasoning survives in reports
    and audit instead of collapsing into a generic "excluded."
  - **Ledger math, not just a new label**: a `REVERSED` row has to *subtract* from a session's count
    and total, not add to them — the two places that computed these totals (`sessions.py`'s
    operator/public views and `reports.py`'s session report) had already drifted into two separate,
    nearly-identical queries before this change; consolidated both into one shared
    `app/domain/ledger.py::compute_ledger_totals`, so there's now one formula instead of two that
    could silently diverge. `excluded_counts` in the report needed no changes at all to show
    "reversed: 1" — it already grouped by any non-accepted decision generically.
  - **Migration `0008`**: additive-only `ALTER TYPE ... ADD VALUE` for Postgres's two native enum
    types (a no-op on SQLite, which has no such type to alter); downgrade is intentionally a no-op
    too, since Postgres has no supported way to drop a single enum label short of recreating the
    type.
  - **Frontend**: the reconciliation queue's resolve buttons gained "Reversed" (opens an inline
    picker of the session's accepted events to choose which one) and "Duplicate" (fires immediately,
    like Exclude already did).
  - **Verified live end-to-end**: seeded an accepted $42.50 contribution and a separate ambiguous
    "reversal notice" event, resolved the reconciliation item as `reversed` picking the $42.50 event
    from the dropdown, and confirmed — without a page reload — the session's live contribution count
    dropped back to 0, the report showed `0` validated contributions and `$0.00`, and the
    excluded-messages breakdown showed both `ambiguous: 1` and `reversed: 1`. Also regression-tested the
    ledger-netting formula itself the same disciplined way as everything else this session: reverted
    the subtraction in `compute_ledger_totals`, confirmed the test went red, restored it.
- *(Historical note, resolved)* This originally flagged that `frontend/` only covered auth/org
  management, with no Session console, Display Studio, reconciliation queue, or reports UI. All of
  those are now built — see the **Frontend** section below for what exists and its own known gaps.
  Every phase above was, regardless, independently verified through direct HTTP calls and the
  automated test suite, so the API itself never depended on the frontend catching up.
- *(Historical note, partially resolved)* This paragraph originally said rate limiting was
  per-process and email-keyed only, with no IP-based throttling. The email-keyed gap (an attacker
  spreading login attempts across many different accounts from one source, none individually
  crossing that account's own threshold) is now closed: a second limiter,
  `login_ip_rate_limiter` (`app/domain/rate_limit.py`), keys on the client's IP with a higher
  ceiling (30 attempts/15 min vs. 10/15 min per email — one IP can legitimately represent many real
  users behind NAT/a shared office network, so it's a broader net, not a tighter one). Checked before
  the email-keyed limiter in `POST /v1/auth/login` (`app/api/v1/auth.py`); only the email-keyed
  counter clears on a successful login, deliberately — clearing the IP counter too would let an
  attacker who eventually guesses one account right reopen the flood gate for every other email from
  the same IP. Still per-process only, and still true that production behind a load balancer with
  multiple workers needs a Redis-backed (or WAF/reverse-proxy-layer) version for state to be shared
  across processes — that half of the original gap remains. New test
  (`test_login_rate_limited_by_ip_across_different_emails` in `tests/test_hardening.py`): registers
  31 distinct accounts, logs into 30 of them (each individually far under its own limiter), then
  shows the 31st trips the shared IP-keyed limiter instead. A new autouse fixture
  (`_reset_login_ip_rate_limiter` in `tests/conftest.py`) clears it between tests, since every test
  client shares the same ASGI-reported IP and would otherwise pollute each other's counters across
  the whole suite. Regression-tested the same disciplined way as everything else in this project:
  reverted the `login_ip_rate_limiter.check()` call, confirmed the new test actually went red (31st
  login returned `401` instead of the expected `429`), then restored it and reconfirmed all 90
  backend tests green. Verified live against a real running server too, end to end with `httpx`
  outside the test suite: 30 wrong-password logins across 30 distinct freshly-registered accounts all
  returned `401`, and the 31st returned `429` — exactly matching the configured threshold.
- *(Historical note, resolved)* This paragraph originally said `mfa_secret` was still stored in
  plaintext, flagged since Phase 1 and made concrete by this phase's own backup/restore drill (a
  stolen backup file was a stolen TOTP seed for every enrolled user). It no longer is: `mfa_secret`
  and `MailboxConnection.webhook_secret` (the HMAC key used to verify inbound webhook signatures —
  also real secret material, unlike `token_ref`, which stores no plaintext token to begin with; see
  the Phase 3 entry above) are now envelope-encrypted at rest with Fernet
  (`app/domain/crypto.py`'s `EncryptedString`, a `TypeDecorator` following the exact pattern
  `app/db/base.py`'s `UTCDateTime` already established for timezone normalization — transparent to
  every call site, encrypt-on-write/decrypt-on-read at the SQLAlchemy layer, nothing above that layer
  changed). The key itself comes from a new `encryption_key` setting alongside `jwt_secret`, same
  "obviously-fake dev default, must be overridden in production" convention. Ciphertext is
  meaningfully longer than plaintext, so both columns were widened (`String(64)` → `String(500)`) in
  migration `0006`, verified both directions (`alembic upgrade head` / `downgrade 0005` / back up
  again) against a real SQLite file. Verified the encryption itself is real (not just "the ORM
  round-trips it," which would pass even with a no-op) by reading the raw column value with a plain
  `SELECT` that bypasses the ORM's decryption — twice: once in the automated suite
  (`test_auth.py::test_mfa_secret_is_encrypted_at_rest` and
  `test_webhook_secret_is_encrypted_at_rest`, asserting the raw value differs from the plaintext, its
  hex encoding, *and* matches Fernet's own always-`gAAAAA...`-prefixed token shape), and again by hand
  against a real running server and a real `letsgive_dev.db` file, not just the in-memory test DB.
  Caught the same way as the `HomeRoute` regression test in the Frontend section: reverted the fix
  and confirmed the automated test actually turned red before trusting it.
- No load/performance testing against spec §12's targets (p95 API response under 400ms, live update
  under 5s) — plausible given the architecture (indexed queries, live aggregates rather than N+1
  loops, a push-based realtime channel) but not measured under actual concurrent load.
- *(Historical note, resolved)* This paragraph originally said there was no CI pipeline. There now
  is: `.github/workflows/ci.yml` runs on every push to `main`/`master` and every PR — a `backend` job
  (`ruff check`, `pytest`) and a `frontend` job (`eslint`, `vitest run`, `npm run build`, which itself
  runs `tsc -b` first). No secrets or `.env` needed for either job: `app/core/config.py` already has
  dev-friendly defaults, the same reason `pytest` has always run cleanly here with no `.env` file
  present. Not yet exercised for real (nothing in this repo has been pushed to GitHub yet), but
  validated by running the exact same commands locally in the exact same order, including a real
  `npm ci` (not `npm install`) to confirm `package-lock.json` is actually in sync after this
  session's dependency additions.

## Frontend

A React SPA in `frontend/` (not the backend's Alembic-migrated territory — a separate app talking
to the API over HTTP). Being built incrementally on top of the already-complete, already-tested
backend; each slice below was verified in an actual browser against the real running API, not just
compiled.

```
frontend/
  src/
    lib/          api.ts (fetch wrapper, JWT bearer auth, typed ApiError), endpoints.ts (typed
                   per-endpoint functions), types.ts (mirrors the backend's Pydantic schemas),
                   useCountdown.ts (client-side ticking timer hook)
    auth/          AuthContext (login/register/logout, current user, token persisted in
                   localStorage), OrgContext (the user's orgs + which one is "active", also
                   persisted), RequireAuth route guard
    components/    DashboardLayout (nav shell + org switcher), ui.tsx (Button/Input/Card/Badge/etc)
    pages/         RegisterPage, LoginPage, MfaSetupPage, CreateOrgPage, OrgHomePage,
                   SessionsListPage, NewSessionPage, SessionDetailPage (the operator console),
                   MailboxSettingsPage (connections + parser profiles), ReconciliationPage,
                   SessionReportPage, AuditLogPage
```

**Stack**: Vite + React 18 + TypeScript + React Router + TanStack Query + Tailwind CSS, with Vitest +
Testing Library for the test suite (`npm test`). Chosen over
Next.js since this is an internal admin/operator tool with no SEO or SSR need — a plain SPA behind
the existing JWT-bearer API is simpler and avoids Next-specific complexity. `vite.config.ts` proxies
`/v1/*` (HTTP and WebSocket) to the backend in dev, so the app talks to relative URLs and needs no
API base-URL configuration.

Manually scaffolded rather than via `npm create vite` — the Node version in this environment
(21.1.0) predates what the current `create-vite` requires, and hand-writing `package.json` /
`vite.config.ts` / `tsconfig.json` was faster than fighting the tool.

### Setup

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /v1 to http://127.0.0.1:8000
```

Run the backend on port 8000 first (see the Backend section above) — the dev proxy target is
hardcoded in `vite.config.ts` since there's only ever one backend to talk to in this environment.

### What's built so far

- **Auth**: register, login (including the two-step MFA challenge — `mfa_required` then a code),
  and MFA enrollment with a **real scannable QR code** (`qrcode` package rendering the backend's
  actual `otpauth://` provisioning URI to a canvas) plus the raw secret for manual entry. Session
  persists across reloads via a `localStorage` JWT, validated against `GET /v1/auth/me` on load.
- **Organizations**: create (blocked client-side the same way the API blocks it — can't submit
  without MFA already enabled, mirroring spec §3.1), an org switcher for users in more than one,
  and a members list + invite form. Role-gated the same way the API is: the invite form only
  renders for Owners, and inviting a Finance/Owner role for a user without MFA surfaces the
  backend's actual validation message, not a made-up client-side one.
- Added `GET /v1/organizations` (list the current user's orgs with their role in each) and enriched
  `MembershipOut` with `user_email`/`user_full_name` **to the backend**, both while building this —
  neither existed before because nothing had needed to render a member list or an org switcher yet.
  Backend tests updated and passing (`test_list_my_organizations_only_returns_active_memberships`).
- **Verified live**, not just compiled: registered a real account, enrolled MFA by actually
  computing a TOTP code from the rendered QR secret and submitting it, created an organization,
  and invited a second real user — first attempting a `finance` role (correctly rejected client-side
  by rendering the backend's "requires MFA enabled" error) then `media` (succeeded, member list
  updated live).
- **Sessions**: a list view, a create form (contribution method, duration, optional mailbox
  connection, test mode), and the full operator console — the single most important screen in the
  product. Every session action lives on one page with role-gated controls that match the backend
  exactly: request-approval and verify (Media), start/pause/resume/extend/close (Media or Finance),
  the amount-visibility toggle (Finance only), a projection-link generator, a live-updating ledger
  events list, and a synthetic-deposit form for `test_mode` sessions. A live countdown timer
  (`useCountdown`) ticks client-side between polls; the whole page polls every few seconds rather
  than using the backend's WebSocket channel, since that channel is public-projection-only (see
  backend gaps) — a real operator realtime channel would let this page push instead of poll.
- Added `GET /v1/organizations/{id}/sessions` **to the backend** (list an org's sessions) — same
  situation as `GET /v1/organizations` before it: nothing had needed a session list before this
  page existed. Backend tests added and passing.
- **Verified live end to end**, playing two roles in the same browser session (signing out and back
  in, since localStorage is shared per-origin across tabs): created a test-mode session as Media,
  requested finance approval, read the OTP out of the server log exactly as a real finance officer
  would receive it, verified, started, simulated a $20 deposit and watched it land in the ledger,
  signed in as Finance to flip the amount-visibility toggle, opened the generated projection link in
  a separate tab and watched it show the live count/amount/timer, then closed the session and
  confirmed the ended state and final ledger persisted correctly.
- **Found and fixed a real, environment-wide bug while doing that live testing**: the operator
  console's countdown showed ~160 minutes remaining on a 10-minute session. Root cause wasn't the
  frontend — the backend was serializing timestamps read back from SQLite *without* a UTC offset
  (e.g. `"2026-09-02T21:11:29"` instead of `"...Z"`), because SQLite (unlike Postgres) silently
  drops `tzinfo` on read. A browser's `new Date(...)` treats an offset-less string as *local* time,
  not UTC, so every timestamp in every API response was wrong by the server's local UTC offset —
  invisibly, and only when something actually rendered a timestamp client-side, which nothing had
  before this page. Fixed at the root in `app/db/base.py`: a `UTCDateTime` `TypeDecorator` that
  normalizes on the way out of the database, replacing every model's raw `DateTime(timezone=True)`
  — one fix covering every timestamp column in the schema, not a patch on this one endpoint. No
  migration needed (it's a Python-layer fix, not a schema change); all 63 backend tests still pass.
- **Found and fixed a real access-control bug in the frontend itself**: the dashboard was wrapped
  in a blanket "must have MFA enabled" route guard, which would have permanently locked out Media
  and Auditor users — spec §3.1 only requires MFA for Owner/Finance. Caught immediately by logging
  in as the Media test user and getting stuck on the MFA setup page. Fixed by moving the MFA
  requirement to exactly where the backend enforces it (creating an organization), rather than
  gating the whole app.
- **Mailbox & parsing settings** (Owner/Finance only, matching the backend): add a mailbox
  connection (the "fake" dev provider only — real Microsoft/Gmail need live OAuth credentials this
  environment doesn't have) and revoke one; add a parser profile (name, comma-separated sender
  patterns, confidence threshold). This is what turns "test mode" sessions into ones that can
  actually receive live deposit notifications.
- **Found and fixed a real functional bug, not just a UX gap, while wiring this up**: the backend's
  `GET .../connections` endpoint required Owner/Finance — but Media is the role that actually
  creates sessions, and the new-session form needs to list connections to let Media pick one. A
  Media user opening "New session" would have hit a silent 403 on that list call. Caught by testing
  the flow as Media rather than assuming the role that built the feature (Owner) was representative.
  Fixed by allowing Media read-only access to the connections list (`app/api/v1/connections.py`) —
  nothing sensitive is in that response (no `token_ref`/`webhook_secret`); create/revoke stay
  Owner/Finance-only. Added `test_media_can_list_but_not_create_or_revoke_connections`.
- Also added an upfront role check on `/sessions/new` itself: previously an Owner (who can't create
  sessions) would see a fully-interactive form that only failed with the backend's raw error message
  after filling it out and submitting. Now matches the pattern already used on `/mailbox`.
- **Verified live end to end** as three different real users in the same browser (signing in and out,
  since localStorage is shared per-origin): as Owner, added a mailbox connection and a parser
  profile through the UI; as Media, confirmed the connection now appears in the session-creation
  dropdown (proving the role fix above), created a real (non-test-mode) session bound to it, ran it
  through the full OTP approval flow, and started it; then confirmed from outside the browser that
  the connection's generated webhook secret actually validates real signed requests (200) and
  rejects forged ones (401) — the same signature-validation path Phase 3's automated ingestion tests
  exercise, now proven wired correctly to a connection created through the UI rather than the API.
- **Reconciliation queue**: pending/resolved tabs, each item showing its reason and the parser's
  detected amount/currency/confidence pulled straight from the stored evidence, with an Accept
  (pre-filled with the detected amount, editable) / Exclude form for Finance — Owner and Auditor get
  read-only visibility, matching the backend's role split exactly.
- **Session reports**: validated count/amount, excluded-message breakdown by reason, corrections,
  full approval history, and mailbox connection health, plus a working CSV export (fetched with the
  JWT bearer header — a plain `<a href>` can't carry that — then turned into a blob download).
- **Audit log**: a straightforward chronological feed of every immutable audit event for the org.
- **Verified live end to end**: seeded an ambiguous ledger event tied to a real live session (the
  same manual-seed technique used to verify this on the backend in Phase 6, since the fake mailbox
  provider is per-process and can't be reached from outside the running server), resolved it as
  Finance through the reconciliation queue UI, watched it disappear from "pending" and reappear
  under "resolved" with the correct resolution and amount, confirmed the session's live contribution
  count updated to reflect the correction, and confirmed the same correction appears in both the
  session report and the audit log — one seeded event, traced correctly through five different
  screens.
- **Accept/decline invitations**, closing a gap that had persisted since Phase 1: previously an
  invited membership just sat at `INVITED` forever with no way for the invitee to activate it short
  of a manual DB edit (which is exactly how every earlier test in this project worked around it).
  Added `GET/POST /v1/me/invitations` **to the backend** (list the current user's pending
  invitations; accept or decline one — decline deletes the row so the org owner can re-invite the
  same person) with tenant-safe ownership checks (only the invitee can act on their own invitation;
  everyone else gets a 404, not a 403, matching the isolation pattern used everywhere else in the
  API). On the frontend, a self-contained `InvitationsCard` renders wherever pending invitations
  exist — above the org home page for an already-active user, or in place of the old unconditional
  "create an org" redirect for a user whose only relationship to Let's Give so far is an invitation.
- **Found and fixed two real bugs while verifying this live.** First, a race condition: accepting or
  declining invalidates both the org list and the invitations list, and the two refetches don't
  always land in the same render — the frame in between (new org not in the list yet, invitation
  already gone) read as "this user has nothing at all" and bounced them to `/organizations/new`
  right after they'd just accepted something. Fixed by only trusting that "nothing at all" reading
  once both queries report settled (`OrgContext` now also exposes `isFetching`), scoped narrowly to
  that one redirect decision. Second, fixing the race the obvious way (gating the whole route on
  either query's `isFetching`) turned out to cause a genuine infinite loop: `InvitationsCard` mounts
  its own subscription to the same query key, React Query's default `staleTime: 0` means every fresh
  mount triggers a background refetch, that refetch flips `isFetching` true, the broad guard
  unmounted `InvitationsCard` in response, the fetch resolved, `isFetching` went false, the card
  remounted, and the cycle repeated — measured at roughly 90 requests/second against the dev
  backend. Caught immediately by watching the network log during live testing, not by inspection.
  Fixed by narrowing the guard to only the specific "0 orgs and 0 invitations" branch, where no
  further mount/unmount of `InvitationsCard` can ever be triggered by it.
- **Verified live end to end** as three real users: invited a fresh user with no prior organization
  and confirmed they land on an invitations-only view (no forced "create org" redirect); accepted the
  invitation and confirmed immediate, correct navigation to the new org's home page with the right
  role badge and membership row, no flash of the wrong screen; separately invited a user who already
  belongs to a different active org and confirmed the invitation banner renders alongside their
  existing dashboard; declined it and confirmed it disappeared with no change to their existing org
  membership; and confirmed a declined invitation can be re-sent by the owner (the row is actually
  gone, not just hidden).

- **Billing**: current plan and subscription status, a grace-period date once canceled, all four
  plans shown with their real limits (sessions/month, mailbox connections, custom subdomain, SSO)
  with the active one highlighted, and a two-step "Cancel subscription" confirm (Owner only,
  matching the backend's role check). No plan-switching UI, because there's no switch-plan endpoint
  on the backend yet — the page says so rather than pretending to offer it.
- **Verified live**: canceled a real trialing subscription through the confirm flow and watched the
  status flip to `canceled` with the correct 30-day grace-period date, consistently on both the
  Billing page and the org Overview page (same `MyOrganization` object, no stale cache between them).

- **Display Studio**: the last major backend-complete surface, and the biggest single piece of UI in
  the frontend so far — a hand-rolled drag/resize canvas editor for `DisplayTemplate`/`DisplayElement`
  (no drag-drop library; plain pointer events, since the interaction is simple: move and one
  corner-resize handle, nothing multi-touch or physics-y). A list page (any role can view; Owner/Media
  can create, duplicate, delete) and an editor page with three panels: an "Add element" palette for
  all twelve element types, the canvas itself (rendered at a fixed 800px preview width scaled from
  the real 1920×1080 canvas, so on-canvas font sizes and positions are proportionally accurate), and
  an Inspector (position/size as both drag and exact numeric fields, font size/color, lock, hide, and
  a text field for the four static-text element types) plus a Layers list for selecting elements too
  small or stacked to click directly. Optimistic-lock save conflicts (409, same pattern as sessions)
  surface as a banner with a one-click reload rather than a lost-work dead end. Finance/Auditor get
  the same editor component in a read-only mode — no palette, no toolbar actions, position/size shown
  as text instead of editable fields — rather than a separate maintained view. Also added a display
  template picker to session creation (`NewSessionPage`), wired through a new
  `display_template_id` field on `CreateSessionPayload` the backend already accepted but no frontend
  form exposed yet.
- **Verified live** as Media: created a template, added a `contribution_count` element and dragged +
  numerically repositioned it, added a `heading` element and set its text live (watched the canvas
  update on keystroke), set a custom canvas background color, marked it default, and saved — then
  confirmed via direct DB inspection that name/canvas/elements/`is_default` all persisted exactly as
  set. Created a new session and confirmed the template picker listed it as "(default)" and the
  session's `display_template_id` was auto-bound with no manual selection, matching the backend's
  auto-bind-to-default behavior. Exercised duplicate (correct "(copy)" name, default cleared) and
  delete (with confirm step) from the list page. Signed in as Finance and confirmed the entire studio
  — list and editor both — is read-only: no New/Duplicate/Delete, no Add-element palette, no toolbar
  save controls, element selection still works and shows position/size as read-only text.
- **Closed the gap flagged at the end of the previous milestone**: the public projection page
  (`backend/app/api/display_page.py`, served at `/display/{session_id}`) used to be a fixed, generic
  layout that never read `session.display_template_id` — a saved Display Studio template had nowhere
  to actually appear. Added `GET /v1/sessions/{id}/display-template/public` to the backend (same
  display-token auth as the WebSocket channel and `.../public`, so no org membership is needed — the
  token itself is the credential) returning the bound template's canvas and elements, or `null` if
  none is bound. The display page now fetches this once on load: if a template with at least one
  element comes back, it renders that template (hand-rolled, matching the Studio editor's own
  rendering logic — no dependency on the frontend's React/npm toolchain, since this page is
  server-rendered vanilla JS/CSS served directly by FastAPI) and scales the whole 1920×1080 canvas to
  fit the browser window via a single CSS `transform: scale()` on load and on resize, rather than
  recomputing per-element positions; if not, it falls back to the original generic layout exactly as
  before, so a session with no template configured is unaffected. Either path uses the *same*
  WebSocket connection for live count/amount/timer updates — the template only changes layout, not
  where the data comes from. `contribution_count`, `amount`, `goal`, `countdown`, and `progress_bar`
  (fill width driven by `total_amount / goal_amount`) update live on every WebSocket push; static
  types (`heading`/`body_text`/`sponsor_message`/`payment_instructions`) render their stored text.
  Also added binding fields the Studio editor didn't have anywhere to set: an "Image URL" field for
  `logo` (rendered as a real `<img>` in both the editor canvas and the public page) and a "QR value"
  text field for `qr_code` (rendered as plain text on the public page for now — not yet a real
  scannable code, since generating one needs a QR library this server-rendered page doesn't load;
  noted below).
- **Verified live end to end**: built a template with all of heading/contribution_count/amount/
  goal/progress_bar/countdown/logo, bound it to a real `test_mode` session with a $500 goal, drove the
  session through request-approval/verify/start via the API, opened the actual `/display/{id}` URL in
  a browser, and confirmed every element rendered correctly on first load (including the logo image
  and a progress bar correctly filled to the exact `total_amount / goal_amount` fraction). Simulated a
  second deposit while the page stayed open and watched count/amount/progress bar/countdown all update
  live with **zero page reload**, purely from the existing WebSocket push. Separately opened a session
  with no bound template and confirmed the original generic layout still renders correctly with no
  regression — the fallback path works exactly as before.
- **`qr_code` elements now render a real scannable QR code**, closing the gap noted at the end of the
  previous milestone. Generated **server-side** in Python (`app/domain/qr.py`, using the `qrcode`
  package) rather than via a client-side JS library: `GET .../display-template/public` renders any
  `qr_code` element's `binding.value` to a PNG and adds it to the response as a `binding.qr_data_uri`
  data URI, computed fresh per request rather than stored — the persisted template only ever keeps the
  raw value string, so there's nothing to invalidate if it changes. Kept the server-rendered display
  page dependency-free and reliable during a live service (no CDN script to fail mid-service) at the
  cost of one Pillow-rendered PNG per request per QR element, which is negligible at this scale. The
  Studio editor got the same treatment independently on the frontend, reusing the `qrcode` npm package
  already used for MFA-enrollment QR codes, so what an operator sees while authoring already matches
  what the congregation will see.
- **Verified live**: added a `qr_code` element with a real URL value in the Studio editor and
  confirmed (via canvas pixel inspection, not just visually) that it rendered actual QR-pattern data,
  not a placeholder; saved, bound it to a session, and confirmed the public `/display/{id}` page shows
  a well-formed QR code (finder patterns present, sized against a white background box for scan
  contrast against the dark theme) generated by the backend endpoint, not the frontend.
- **Four starter template presets**, so a new org doesn't have to build a layout from a blank
  canvas: Minimal Count (just the number, large and centered), Goal Thermometer (a progress bar plus
  amount and count side by side), Bold Announcement (large logo and amount for a milestone moment),
  and QR & Give (payment instructions and a scannable QR code next to the running count, for a live
  offering/giving moment). Purely a frontend addition (`frontend/src/lib/templatePresets.ts`) — no
  backend or schema changes, since a preset is just a canvas + element list run through the existing
  create-then-save calls the "blank template" button already used. `DisplayStudioListPage`'s "New
  template" button now opens a small picker (blank, or one of the four presets, each with a live
  color-swatch/element-count preview) instead of always creating blank; picking a preset creates the
  template, immediately saves it with the preset's canvas and elements, and navigates straight into
  the editor exactly as the blank-template flow already did — so it fits the existing
  create/duplicate/delete/save machinery with no new endpoints. **Verified live end to end**: created
  both the Goal Thermometer and QR & Give presets as a real Owner against a real running backend, and
  confirmed in the actual Studio editor canvas — not just by reading the JSON — that every element
  landed at the right position with the right styling (the goal/amount/count colors, the left-aligned
  QR & Give layout, the correct canvas background per preset) and that a reload round-trips the saved
  data unchanged.
- **Added dedicated test coverage for the preset picker** (`frontend/src/pages/DisplayStudioListPage.test.tsx`,
  5 tests), closing a real gap the previous milestone left open — the feature had only been checked
  by hand in the browser. Covers: the picker being completely absent for a role that can't edit;
  opening it shows a "Blank template" option plus all four presets by name and description; picking
  "Blank template" calls only `create()` (never `save()`) and navigates to the new template; picking
  a preset calls `create()` then `save()` with exactly that preset's canvas/elements before
  navigating; and a failed `create()` surfaces the backend's error message and does *not* navigate
  away. `useOrg` and `displayTemplateApi` are mocked directly (no need to bootstrap real
  Auth/OrgProvider for a page-level test that only cares about role-gating and the mutation calls),
  following the same `vi.mock` pattern `App.test.tsx` already established.
- **Preset and saved-template cards now show a real rendered preview of the canvas, not a generic
  color swatch with an element count.** Extracted the Display Studio editor's own per-element layout
  math (`previewText`, the position/size/color/font resolution) into a shared
  `frontend/src/lib/templateElementStyle.ts`, so the editor and this new preview can't drift apart on
  what a template actually looks like. Added a percentage-based variant
  (`elementPreviewStylePercent`) specifically for the preview case, since a grid card's width is
  responsive and unknown ahead of time — plain CSS percentages (plus an `aspect-ratio` matched to the
  template's own canvas dimensions, not a fixed 16:9 assumption) handle that without a
  `ResizeObserver`; font size intentionally stays a small constant rather than scaling with the
  canvas, since at preview size the layout/composition is what matters, not proportionally exact
  type. The new `frontend/src/components/TemplateCanvasPreview.tsx` component is used in both places
  the old swatch was: the preset picker and the list of an org's own saved templates. 8 new unit
  tests (`templateElementStyle.test.ts`) cover the placeholder-text rules (including the logo/QR
  "only show a fallback label when there's no real binding value yet" case) and the percentage
  conversion math; regression-tested the same disciplined way as everything else in this project —
  reverted the `* 100` in the percentage conversion, confirmed the test failed with a readable
  `'0.25%' to be '25%'` diff, then restored it. **Verified live**: registered a real account, enabled
  MFA, created an org, and opened Display Studio in the browser — all four presets render their
  actual distinct layouts (the countdown badge and centered count for Minimal Count, the progress bar
  and side-by-side amount/count for Goal Thermometer, the logo placeholder and sponsor banner for
  Bold Announcement, the QR placeholder and left-aligned payment instructions for QR & Give), then
  created a real template from a preset and confirmed the saved-template card renders the identical
  preview. Also confirmed the previews render correctly under both the light and dark app themes
  (the canvas itself intentionally stays at its own designed background regardless of app theme,
  same as the real public display).
- **Added first brand assets**: a simple heart-in-badge mark (`frontend/public/favicon.svg`, brand
  blue `#254dd1` background, white heart) as the browser-tab favicon, and a reusable
  `frontend/src/components/BrandMark.tsx` icon+wordmark lockup used everywhere the app names itself
  — the dashboard header and all three auth-card pages (Login, Register, MFA setup) — so a future
  logo refresh only touches one file. The badge is a fixed brand color in both themes (it's the
  literal logo, not theme-sensitive chrome); the wordmark text still uses the existing
  `dark:text-brand-400` pattern. The standalone public display page (`backend/app/api/display_page.py`,
  served straight from the backend with no access to the frontend's static assets) gets the same
  mark as an inlined base64 data URI for its own favicon, substituted in via a placeholder token
  rather than embedded directly in the page template so its one long base64 line didn't blow past
  the line-length linter. Verified live in the browser at both `/login` and `/register`, in both
  light and dark mode, and confirmed `document.querySelector('link[rel="icon"]').href` resolves to
  the real asset (not a 404) after a production build.
- **Plan switching**: `POST /v1/organizations/{id}/subscription/switch-plan` (Owner only) changes plan
  immediately, either direction, audit-logged as `subscription.plan_changed` with before/after plan
  ids. A downgrade doesn't retroactively touch anything the org already has over the new plan's
  limits — same as how entitlement checks already only gate *new* creation
  (`app/domain/billing.py`) — the new limits just apply going forward. Blocked once the subscription
  is canceled (`400`, same "contact support" framing as the rest of the cancel flow, since there's no
  reactivate endpoint yet). The billing page's "Switching plans isn't available yet" placeholder is
  gone — each non-current plan card now has a "Switch to this plan" button with an inline confirm
  step (billing actions get a confirm, same pattern as canceling).
- **Verified live**: switched a trialing Starter org to Growth as Owner, confirmed the current-plan
  card and badge updated immediately and `subscription.plan_changed` appeared correctly in the audit
  log; then canceled the subscription and confirmed every "Switch to this plan" button disappeared
  in favor of the "contact support to reactivate" note, matching the backend's `400` guard exactly.

- **Operator console is now realtime, not polling.** `SessionDetailPage` used to refetch
  `GET .../operator` every 4s and `GET .../events` every 6s. It now gets pushed live updates over a
  new `WS /v1/sessions/{id}/live-operator` channel (`useOperatorSocket.ts`), the same broadcaster
  the public display already used, just a second topic (`{session_id}:operator`) carrying the fuller
  operator payload (`version`, `operator_warning`, etc. — fields the sanitized public channel never
  gets). Auth is a short-lived, session+user-scoped "operator socket token"
  (`create_operator_socket_token` in `app/core/security.py`), minted over a normal authenticated
  REST call and passed in the WS query string — mirroring exactly why the public channel already
  does this (a browser WebSocket handshake can't carry a custom Authorization header), but never
  putting the general-purpose access token itself in a URL. Re-verifies active Media/Finance
  membership at connect time, not just token validity, so someone removed from the org doesn't keep
  a live feed for the token's full hour. The 4s poll is still there, just widened to 20s, as a dumb
  safety net in case the socket silently stalls; the header now shows a small "connected" /
  "reconnecting…" badge so an operator can tell at a glance.
- **Found and fixed while testing this as an Owner** (not just Media, the role that built the
  feature): `GET /sessions/{id}/operator` and `GET /sessions/{id}/events` were both Media/Finance-only
  on the backend, but `SessionsListPage` links *every* role to `/sessions/:id` — so an Owner or
  Auditor clicking through from their own sessions list got a confusing "Session not found" instead
  of a read-only view, and (separately) the events list 403'd silently in the background forever.
  Both endpoints are now read-only for any active member, matching how reports/reconciliation already
  work — control actions (start/pause/close/etc.) stay exactly as Media/Finance-only as before. Also
  hid the "Get projection link" button from non-operator roles on the frontend, since minting a
  display token was already Media/Finance-only server-side and the button was a dead end for anyone
  else. Added test coverage for the fix (`test_owner_and_auditor_can_view_but_not_control_session_operator_state`).
- **Verified live**: opened the session detail page as Media in one browser tab, then ran a
  `simulate-deposit` call from a completely separate script — the contribution count updated from 0
  to 1 in the open tab with zero clicks, zero reload, purely from the WS push. Confirmed the "live" /
  "connected" badges read unambiguously (an earlier draft had both say "live," which was confusing).
  Then logged in as Owner and confirmed the page now loads correctly instead of 404ing, with no
  control buttons shown and no background request errors.
- **Frontend test suite** (`vitest` + `@testing-library/react`): `npm test` from `frontend/`. 27
  tests across 4 files, all passing, plus `tsc -b`, `eslint .`, and `npm run build` all still clean.
  Not exhaustive page-by-page coverage — this establishes the pattern (`vitest.config.ts`,
  `src/test/setup.ts`) and covers what actually mattered from this session's own history:
  - `lib/api.test.ts` — the `apiRequest` wrapper: JSON success, 204 handling, bearer-token
    attachment (and omission with `auth: false`), and every error-body shape it has to turn into an
    `ApiError` message (string detail, FastAPI-style validation array, missing detail, non-JSON body).
  - `lib/useCountdown.test.ts` — m:ss formatting, zero-padding, ticking via fake timers, clamping to
    `0:00` instead of going negative.
  - `lib/useOperatorSocket.test.tsx` — against a hand-rolled `FakeWebSocket` (jsdom has no real WS):
    connects with a freshly-minted token, reports connected/disconnected on open/close, pushes
    messages into the `["session", id]` query cache, ignores a malformed frame instead of throwing,
    and closes (without reconnecting) on unmount.
  - `App.test.tsx` — a **regression test for the `HomeRoute` redirect race** documented above. Worth
    calling out on its own: the first version of this test didn't actually catch the bug it was
    written for (it modeled an *initial-load* race, but the real bug was specifically a *refetch*
    race — `isLoading` is false throughout a refetch of already-cached data, only `isFetching` flips).
    Caught by deliberately reverting the fix and confirming the test still passed — a reminder that a
    regression test needs its own verification pass (revert the fix, confirm red; restore it, confirm
    green) or it's just decoration. The corrected version does that round-trip and reproduces the
    exact stale frame: org list mid-refetch (`isFetching: true`, still showing pre-refetch data) while
    invitations has already resolved to empty, and asserts `HomeRoute` shows a spinner rather than
    redirecting.
- **Fixed the dashboard nav's narrow-width wrapping** (previously a noted known gap): at narrow
  widths, both header rows (`DashboardLayout.tsx`) would squeeze until link/button text wrapped onto
  multiple lines — "Sign out" splitting into "Sign" / "out", the nav tabs stacking three rows deep.
  Root cause was missing `whitespace-nowrap` on the text, not `flex-wrap` on the container (the
  container was already `nowrap` by default). Fixed by adding `whitespace-nowrap` +
  `flex-shrink-0` throughout and `overflow-x-auto` on both rows, so a too-narrow viewport scrolls the
  row horizontally instead of wrapping text inside squeezed items; the email additionally truncates
  at a fixed max-width rather than scrolling, since reading it in full is rarely necessary. Verified
  live at the browser pane's ~657px default (both rows now single-line and horizontally scrollable,
  confirmed "Sign out" and "Billing" are reachable by scrolling) and at 1280px (unchanged from
  before — everything still fits on one line with no scrollbar).

### Known gaps

- *(Historical note, resolved)* This paragraph originally said there was no route-level code
  splitting. There now is: every page except `OrgHomePage` (kept eager since `HomeRoute` renders it
  directly, not just as a route element, and it's the landing page nearly every session hits first
  anyway) loads via `React.lazy()` behind a single top-level `<Suspense>` in `App.tsx`, instead of one
  monolithic bundle. Concrete, measured effect on `npm run build`'s output: the main chunk shrank
  from 305 KB to 227 KB (93 KB → 72 KB gzipped), with the rest split into 15 small per-route chunks
  (1–12 KB each) that only load when a session actually visits that page — `DisplayStudioEditorPage`,
  the heaviest at 12 KB, pulls in the `qrcode` library that most sessions never touch. Verified live
  by navigating through every lazy route (login, sessions, billing, mailbox, reconciliation, and the
  Display Studio editor itself) after a fresh page load and confirming each rendered correctly with
  no console errors — not just that the build produced smaller files.
- *(Historical note, resolved)* This paragraph originally said dark mode was open, alongside i18n,
  as a subjective decision better left to the user. Dark mode is now implemented; i18n remains open
  for the same reason as before (it needs a target-language decision only the user can make).
  Implementation: `tailwind.config.js` sets `darkMode: "class"`; a small inline script in
  `index.html`'s `<head>` reads `localStorage["letsgive.theme"]` (falling back to
  `prefers-color-scheme`) and toggles the `dark` class on `<html>` before first paint, so there's no
  flash of the wrong theme on load. `ThemeContext.tsx` (new, mirroring the existing
  `AuthContext`/`OrgContext` pattern) owns the live toggle state, persists the choice back to
  `localStorage`, and is provided at the top of `main.tsx`. `DashboardLayout.tsx` got a sun/moon
  toggle button in the header. Every shared primitive in `components/ui.tsx` (`Button`, `Input`,
  `Label`, `Card`, `ErrorText`, `Badge`'s five tones) and every page/component with its own
  hand-written Tailwind classes got paired `dark:` variants for backgrounds, text, borders and
  dividers, following a consistent slate-scale mapping (e.g. `text-slate-500` ->
  `dark:text-slate-400`, `bg-white` -> `dark:bg-slate-800`). One deliberate exception: the Display
  Studio editor's canvas preview (`DisplayStudioEditorPage.tsx`) keeps its inline-styled colors
  untouched by the app theme, since those represent the projection template's own design (background
  color, element colors, the QR preview's white backing), not application chrome. Verified live: a
  full run through registration, MFA setup, org creation, mailbox connection, and the dashboard,
  sessions, mailbox, display studio, billing, audit and reconciliation-access-gate pages, plus
  toggling the switch, confirming it persists across a reload, and confirming it renders correctly on
  first load with no theme flash.
- The public `GET /display/{id}` projection page (built in backend Phase 4) is intentionally
  separate from this app — plain server-rendered HTML with no build step, which is the right shape
  for a page an OBS Browser Source points at. It isn't going to be ported into the React app.
