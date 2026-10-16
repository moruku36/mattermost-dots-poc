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

> **Scope Note**: This public pull request records only the non-secret architecture, root-cause analysis, and synthetic verification results. The executable replacement candidate code (`windows_pat_candidate.py`) is maintained exclusively in an isolated local candidate directory outside the public repository.

The local replacement candidate introduces `replace_credential()` and `WindowsStore.replace_credential_and_policy()` adhering to the following strict safety conditions:

1. **Explicit Confirmation**: Requires explicit confirmation (`confirm_replace=True` / `--replace-expired`) in addition to an interactive masked terminal session.
2. **Process Cessation Attestation via Shared Trial Lock & Runtime Receipts (`hold_poc_stop_lock` / `verify_poc_runtime_stopped`)**:
   - Instead of relying on process names, window titles, or user self-reports, the replacement candidate connects to the existing PoC lifecycle synchronization primitives:
     - **Exclusive Byte-Range Lock**: Acquires and holds the non-blocking OS byte-range lock (`msvcrt.locking(..., msvcrt.LK_NBLCK, 1)` at offset 0) on the existing shared `trial.lock` file for the entire duration of `replace_credential()`. If `trial.lock` is missing, is a reparse point, or is currently locked by a running trial, replacement fails closed (`POC_STOP_UNVERIFIED_REFUSED`).
     - **Quarantine Marker Check**: Verifies before and after lock acquisition that `trial.lock.blocked` (`LOCAL_CLEANUP_UNCONFIRMED`) does not exist.
     - **Runtime Completion Receipt Verification**: Inspects the PoC runtime directories (`run-poc-*` and `run-*`) while holding `trial.lock`, requiring that at least one runtime root exists and every existing run subdirectory has completed with confirmed stop receipts (`poc-owner-result.json` / `poc-tunnel-result.json` with `source_job_stopped: true` and `callback_values_cleared: true`, `poc-tunnel-diagnostic.json` at `stage: "WRITE_RESULT_DONE"` and `confirmation: "TRUE"`, no `.pending` diagnostic file, and `trial-result.json` for legacy trial runs).
3. **Revocation & Owner Verification**: Rejects replacement if the `revoked` marker exists or if the new policy owner does not match the existing stored policy owner.
4. **Objective Cryptographic & Expiry Verification (`verify_payload` Alignment)**:
   - In `verify_payload()`, timestamp expiry (`if expires <= now: raise Denied("Credential expired; store a new one")`) is evaluated after JSON schema, version, SID, owner, `policy_sha256`, and lifetime checks, and immediately before `validate_token()`.
   - `replace_credential()` first calls `verify_payload(..., now=now, include_expiry=True)`:
     - If no exception is raised, the credential is still active and replacement is refused (`Denied("Existing credential has not expired yet")` -> `CREDENTIAL_STILL_VALID`).
     - If `Denied` is raised with any message other than `"Credential expired; store a new one"`, replacement is refused (`CREDENTIAL_UNVERIFIED`).
   - Next, `replace_credential()` runs a second `verify_payload(..., now=created_at, include_expiry=True)` probe on a copy of the decrypted payload so that `validate_token()` and all bound fields (SID, owner, `policy_sha256`, timestamps) are cryptographically verified even when expired. Self-reported expiry alone never bypasses payload verification.
5. **Two-File Staging, Post-Verification & Automatic Rollback (`WindowsStore.replace_credential_and_policy`)**:
   - Because `WindowsStore.create()` enforces `CREATE_NEW` and refuses to overwrite existing files, `replace_credential_and_policy()` stages `.tmp` (new data) and `.bak` (existing `policy.json` and `credential.bin` bytes) under `STAGE_FILES` with protected ACLs.
   - It then executes two `_replacer` (`os.replace`) operations (`policy.json.tmp -> policy.json`, `credential.bin.tmp -> credential.bin`), followed by byte-level comparison and the `verify_after` callback (which re-loads `policy.json` and decrypts/verifies the newly stored `credential.bin`).
   - If the second `os.replace`, byte comparison, or `verify_after` fails, the exception handler automatically restores `policy.json.bak -> policy.json` and `credential.bin.bak -> credential.bin` via `_replacer`, cleans up staging files, and verifies that both files match the original bytes (`old_policy` and `old_cred`).

---

## 4. Verification & Testing Boundaries

* **Synthetic 6-Path Replacement & Rollback Verification (In-Memory Fake Store/DPAPI/ACL)**:
  1. Missing explicit confirmation (`confirm_replace=False`) -> refused (`CONFIRMATION_REQUIRED`), original 2 files preserved.
  2. Unverified or false process stop attestation (`None` / `False`) -> refused (`POC_STOP_UNVERIFIED_REFUSED`), original 2 files preserved.
  3. Owner mismatch, `revoked` marker present, or tampered payload token -> refused, original 2 files preserved.
  4. Unexpired existing credential (`now < expires_at`) -> refused (`Existing credential has not expired yet`), original 2 files preserved.
  5. Expired valid credential with matching owner, valid SID/digest, and stop attestation -> replaced and post-verified successfully.
  6. Failure injected on the 2nd `os.replace` (after `policy.json` was already replaced) and failure injected during post-replacement `verify_after` -> automatic `.bak` rollback restored both `policy.json` and `credential.bin` to exact original bytes.
* **Synthetic 4-Path Stop-Lock & Runtime Receipt Differential Verification**:
  1. Missing or busy `trial.lock` -> refused (`shared trial lock is missing` / `shared trial lock is busy`), original 2 files preserved.
  2. Quarantine marker `trial.lock.blocked` present -> refused (`shared trial lock is blocked`), original 2 files preserved.
  3. Active/incomplete `run-poc-*` (missing `poc-tunnel-result.json` / `poc-owner-result.json`) or missing runtime directories -> refused (`active or unconfirmed trial state` / `runtime state unavailable`), original 2 files preserved.
  4. Available `trial.lock`, no `.blocked`, and confirmed `run-poc-*` receipts -> lock held throughout both `_replacer` file writes (`[True, True]`) and released in `finally`.
* **Strict Safety Boundaries Maintained**:
  * Real DPAPI / credential store / lock files / filesystem: untouched during synthetic testing.
  * Real PAT / tokens: never input, read, printed, or logged.
  * Live WebSocket / network connection: unexecuted.


