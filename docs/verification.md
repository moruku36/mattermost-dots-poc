# Verification and evidence boundaries

## WebSocket change: October 9, 2026

Implemented on Windows with Python 3.11.9 against public main `c1828da9` in an isolated checkout. No repository AGENTS.md, CONTRIBUTING or repository skill files were present.

**40 tests PASS, no skips**, plus the synthetic demo: `delivered=1`, `active_after_expiry=0`, `callback_values_after_expiry=0`, `network_calls=0`.

| Evidence | What was checked | What it does not prove |
| --- | --- | --- |
| Fake-clock/source tests | posted provenance, other-channel exclusion, body removal, duplicate/reconnect/five-event budgets, three-attempt backoff, fixed TTL, wall-clock rollback, expiry, revocation, auth timeout, malformed/oversize/message bounds | Live Mattermost interoperability |
| Adapter tests | Fixed WSS endpoint, Authorization header, verified TLS options, disabled logging/proxy/compression, refusal of redirects/401/403 and oversize protocol close | A real WSS/TLS handshake to Mattermost |
| Loopback WebSocket fixture | Real framing and header handshake, hello/posted, normal disconnect, reconnect, duplicate and other-channel discard | Real Mattermost, TLS or external delivery; fixture is deliberately plain WS on loopback |
| Native/queue tests | No source connection at discovery; verified subscription starts source; signed metadata-only callback fixture; unsubscribe/finish, process restart refusal and state clearing; DPAPI dummy roundtrip; dedup/retry/expiry | A connected ChatGPT conversation wake |

The package distribution host failed TLS negotiation in this Windows environment. Certificate verification was not disabled. The suite ran using the official `python-websockets/websockets` tag `15.0.1`, commit `37c9bc0781f0cc5af7c729947ef1833c1e12b70d`, via a local source path. `requirements.txt` and CI pin `websockets==15.0.1`; installation of the distributed wheel was not verified here. No third-party source is included in this change.

**No real connection was attempted.** None of `MM_BASE_URL`, `MM_TOKEN`, `MM_CHANNEL_ID`, `MM_TRIAL_APPROVED`, `MM_NATIVE_RUN_DIR` was present in the executing owner environment. No secret files were searched or read, no credentials were generated, and no channel permissions were expanded. A live trial still needs the existing approved origin/channel/token injected by the regular owner launcher, explicit finite-trial approval, and the already authorized MCP host/client supplying a verified callback. Real source receipt, signed external delivery and ChatGPT wake remain unverified.

The authenticated socket can transiently receive other token-visible channel events. Local filtering is not a server-side permission restriction. A disconnect records `possible_gap=true`; reconnect never claims replay or continuity. Five unique events and the original finite deadline are shared across connections. Process restart refuses source resumption.

## Historical owner observation

On October 8, 2026, the private supervised REST trial reportedly observed one post, one delivered event, a matching ChatGPT wake, 55 REST reads and expiry cleanup. Its offline suite reported 49 PASS. The original public REST export reported 34 passing Windows tests. These historical results do not verify this WebSocket change. Actual identifiers, callbacks, keys and raw logs remain excluded.

## Publication boundary

Only an explicit source/test/docs/workflow/dependency allowlist is staged. Publication excludes ignored runtime state, local library source, environments, credentials, callbacks, DBs and machine-specific paths. A scanner checks staged paths and staged text for credential/private-key patterns and local paths without printing matching values. Dummy keys and identifiers remain explicitly synthetic. No external message, community reply or merge is part of this change.
