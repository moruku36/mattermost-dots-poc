# Architecture

| File | Responsibility |
| --- | --- |
| `poc_config.py` | One configured channel; an obvious synthetic default for tests |
| `notification_core.py` | Bounded strict JSON and fixed refusal type; no message-body draft path |
| `public_runtime.py` | Public owner configuration, exclusive process markers, fixed-channel REST GET adapter |
| `source_scope.py` | Poll interval, finite deadline/read budget, metadata extraction and possible-gap stop |
| `mcp_events_candidate.py` | Discovery/schema, verification challenge, signing, finite subscription, dedup/retry queue |
| `protected_delivery.py` | Windows DPAPI and public-address-pinned HTTPS callback transport |
| `native_service.py` | JSON-RPC stdio lifecycle, poll start after subscription, unsubscribe/EOF cleanup |
| `demo.py` | Synthetic offline end-to-end event + expiry exercise |

The client subscribes on the same authenticated MCP endpoint used for discovery. Its callback verification must succeed before monitoring begins. A single deadline is shared by subscription and source polling; requesting a longer TTL cannot expand the five-minute maximum.

The source reads a recent snapshot of at most five posts. Bodies can be present in transient HTTP memory. The public REST adapter discards body/author/team/attachment fields before returning the three source identifiers; `source_scope` normalizes them to exactly `channel_id`, `post_id`, `created_ms`. A full new-post page is a possible gap, so polling stops rather than fetching more history.

Event IDs are deterministic for a channel/post pair. The queue reserves a delivery attempt before sending so retry counts do not silently reset. Delivery is at-least-once; this is not an exactly-once claim. The callback must verify signatures and deduplicate the stable event ID. Subscription/signing state is protected with Windows DPAPI and cleared on cancellation/expiry; delivered metadata may remain in the local runtime DB.

The live template trusts an authenticated single-owner stdio host. It is not a remote authentication server. The original private supervisor, machine-specific pins, owner identities, runtime approvals, credential backends and tunnel configuration are excluded and must not be copied blindly.

## Public-export differences

- Machine-specific owner/backend imports were replaced with `public_runtime`.
- Incoming-webhook reply-draft code was excluded: this export concerns read polling and metadata delivery only.
- Tests use synthetic owners, posts and callback fixtures. Raw historical evidence is not distributed.
- No private Git history, credential files, execution logs, callbacks, real identifiers or local paths are included.

The Mattermost official MCP server remains a separate existing product. Equivalent event support has not been assessed by this experiment.
