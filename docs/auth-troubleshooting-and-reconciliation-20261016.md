# Mattermost MCP Adapter: Authentication Troubleshooting and Credential Reconciliation Design

## 1. Executive Summary & Objective

This document records the technical investigation, identified blockers, and safety designs for personal access token (PAT) registration and credential lifecycle management within the local read-only Mattermost MCP adapter trial.

All findings are documented using non-secret identifiers, generalized environment paths, and objective evidence collected without exposing sensitive tokens, credentials, or production message payloads.

---

## 2. Identified Authentication Blockers & Root Causes

### 2.1 Disabled Registration CLI Entrypoint
* **Observed State**: Execution of `windows_pat_candidate.py` directly output `{"status":"disabled"}` and exited.
* **Confirmed Cause**: The integration candidate intentionally suppressed the standalone credential registration CLI to prevent untrusted execution in non-interactive environments and avoid unformatted exception leakages.
* **Remediation**: Implemented strict interactive terminal checking (`sys.stdin.isatty()` and `sys.stderr.isatty()`) and sanitized structured JSON diagnostic responses (`_SAFE_CLI_REASONS`), allowing safe masked terminal prompt registration (`getpass`) without exposing secrets.

### 2.2 Re-registration Refusal Due to Existing Credential (`Manual Reconciliation Required`)
* **Observed State**: Calling `store_credential()` when existing files exist in `%LOCALAPPDATA%\<STORE_NAME>` raises `Denied("Existing storage requires manual reconciliation")`.
* **Confirmed Cause**: The original implementation had no credential rotation or replacement interface. Any existing credential (even if expired or invalid) halts further automatic storage writes to prevent inadvertent overwrite or race conditions.
* **Remediation Design**: Formulated a dedicated `replace_credential()` routine that enforces atomic replacement with rollback, requiring explicit owner matching, objective cryptographic expiration proof, and process cessation verification.

### 2.3 Privilege Separation: Server PAT vs. Local Adapter Policy
* **Mattermost Server PAT**: Inherits full user account permissions on the Mattermost instance. Fine-grained channel scoping or automatic short-lived expiration is not natively enforced by standard Mattermost PAT creation.
* **Local MCP Adapter Policy**: Strict confinement is enforced client-side via `approved-policy.json` (strictly restricting reads to `<SINGLE_APPROVED_CHANNEL>`, forbidding post actions, and setting maximum event bounds).

---

## 3. Safe Credential Replacement Architecture

The new candidate introduces `replace_credential()` adhering to the following strict safety conditions:

1. **Explicit Confirmation**: Requires both command-line confirmation (`--replace-expired`) and an interactive masked session.
2. **Owner Verification**: Validates that the newly presented policy owner exactly matches the existing stored policy owner.
3. **Objective Expiration Verification**: Checks the internal cryptographic expiration timestamp of the existing DPAPI payload. Self-reported expiration is rejected. If the existing credential is still valid, replacement is refused (`CREDENTIAL_STILL_VALID`).
4. **Process Cessation Attestation (Current Blocker)**:
   - To prevent race conditions or simultaneous child process usage, credential replacement must objectively verify that no running adapter process is accessing the store.
   - *Current Implementation Status*: Because an external OS-level process cessation attestation interface is not yet linked in the local environment, the replacement candidate maintains a fail-safe refusal state (`POC_STOP_UNVERIFIED_REFUSED`).
5. **Atomic Replacement & Rollback**: Writes new credentials to a `.new` temporary store and applies `os.replace` atomically. In the event of I/O failure, the previous credential and policy are fully restored.

---

## 4. Verification & Testing Boundaries

* **Synthetic Verification**: Offline validation confirmed that non-interactive invocations are rejected with `TERMINAL_REQUIRED` (exit code 2), and missing attestation interfaces safely halt with `POC_STOP_UNVERIFIED_REFUSED`.
* **Strict Safety Boundaries Maintained**:
  * Real DPAPI / credential store: untouched during analysis.
  * Real PAT / tokens: never printed, logged, or handled by automated assistants.
  * Live WebSocket / network connection: completely unverified and uninitiated.
