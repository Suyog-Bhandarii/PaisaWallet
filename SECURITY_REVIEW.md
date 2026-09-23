# Paisa Wallet — Security & Financial Hardening Review

Date: 2026-09-23

## Scope

The review covered the Flask application, SQLAlchemy models, wallet/accounting service, idempotency handling, authentication/session flow, templates/static frontend, migrations, tests, and deployment configuration.

## Implemented controls

- Atomic idempotency reservation using a database uniqueness constraint and transactional response persistence.
- Wallet row locking with deterministic user ordering for PostgreSQL.
- In-process serialization for SQLite development/test environments.
- Daily velocity checks evaluated while the relevant wallet rows are locked.
- Double-entry ledger shape validation before financial commits.
- Append-only ledger and audit-log ORM protections.
- Admin-only, auditable, exactly-once transaction reversal.
- POST-only CSRF-protected logout.
- Dedicated rate limits for financial write endpoints.
- Generic readiness errors without database exception disclosure.
- Explicit trusted-proxy configuration instead of trusting arbitrary `X-Forwarded-For` headers.
- CSP nonces for scripts and no third-party script/font origins.
- Explicit application/database state enums and database CHECK constraints.
- PostgreSQL integration/concurrency test environment.
- Failure-injection and security regression tests.

## Important deployment assumptions

1. SQLite is for development/test use only. It is not the production concurrency model.
2. Production should use PostgreSQL and a shared rate-limit backend.
3. `TRUSTED_PROXY_HOPS` must match the actual trusted reverse-proxy chain; setting it incorrectly can affect client IP attribution.
4. The NPR 5,000 signup credit and API top-ups are simulated wallet operations. They do not represent real banking rails or external settlement.
5. Real-money production use would additionally require regulatory, KYC/AML, fraud, dispute, settlement, key-management, operational-resilience, and external payment-rail controls that are outside this demo application's scope.

## Verification limitation in this build environment

The source tree was syntax-checked with Python AST/bytecode compilation. Full runtime tests could not be executed in the provided environment because Flask and the project's third-party dependencies were not installed and outbound package installation was unavailable. CI is configured to install dependencies and run both the normal and PostgreSQL suites.
