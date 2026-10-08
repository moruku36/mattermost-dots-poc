# Verification and evidence boundaries

## Historical owner observation

On October 8, 2026, the private supervised trial observed one new post, one delivered event, a matching ChatGPT conversation wake, 55 REST reads, expiry cleanup with zero active subscriptions and cleared signing values. Its offline suite reported 49 PASS. These counts are the owner's recorded evidence, not a new measurement of this export. Actual identifiers, callback addresses, secret material and raw logs are excluded.

## Public-export checks

The final public core/polling/protected transport/service/REST-adapter suite was run on Windows with Python 3.11: **34 tests PASS**, no skips, no live Mattermost or ChatGPT connection. It includes a synthetic source and a mocked callback transport, callback verification/signatures, metadata schema, dedup/retry bounds, expiry/unsubscribe, source budget and DPAPI fixture storage. The offline demo also passed with one delivered fixture event and zero active subscriptions/callback values after expiry.

Additional public-adapter and demo checks are run before publication and in the repository CI. On Linux, Windows-only DPAPI/native-service tests are intentionally skipped and must not be counted as live-path verification.

The public REST adapter and owner setup replace private code. Their offline contract tests do not establish live interoperability, production readiness, automatic connection setup, always-on operation, reliable restart recovery or two-way chat. No new live trial was run during export.

## Publication review

Only an explicit source/test/docs/workflow allowlist is committed. No original `.git`, private runtime directory, logs, credentials, callbacks, owner SID or machine-specific path is copied. A pre-publication scanner checks this allowlist and known private scope values without printing those values. Synthetic examples remain clearly marked.

Code is owner-authored and depends only on Python standard-library modules; there are no bundled SDKs, server sources, tunnel binaries or third-party packages. MIT licensing covers this exported source. Product and protocol references are links to their official documentation.
