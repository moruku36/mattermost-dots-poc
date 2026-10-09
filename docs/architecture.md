# Architecture

| File | Responsibility |
| --- | --- |
| `poc_config.py` | One configured approved channel |
| `notification_core.py` | Bounded strict JSON; fixed refusals |
| `public_runtime.py` | Owner preflight, exclusive markers, authenticated WSS adapter |
| `source_scope.py` | Finite posted-event reception, scope filter, dedup, reconnect budgets |
| `mcp_events_candidate.py` | MCP schema, callback verification/signing, finite subscription, retry queue |
| `protected_delivery.py` | Windows DPAPI and pinned public-address HTTPS callback transport |
| `native_service.py` | Stdio lifecycle, connect after subscription, cancel/EOF/expiry cleanup |
| `demo.py` | Synthetic offline event + expiry exercise |

A successful subscription and callback verification start the source. The shared deadline cannot exceed five minutes or the requested shorter TTL. A monotonic source timer also prevents a wall-clock rollback from extending socket monitoring. Discovery starts no source connection. This remains a single-owner authenticated stdio template.

The adapter uses the existing token in the Authorization header at `wss://<configured-origin>/api/v4/websocket`. TLS hostname verification stays enabled; proxies, redirects, compression and WebSocket library logging are disabled. An authenticated `hello` is required within five seconds. [Mattermost documents header authentication and WebSocket events](https://docs.mattermost.com/api/reference/mattermost-api); [the endpoint](https://docs.mattermost.com/api/reference/connect-web-socket) is fixed. The pinned [websockets 15.0.1 sync client](https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html) supplies framing, bounded messages and timeouts.

Mattermost may broadcast events from all channels visible to that token. This code does not join channels or request additional subscriptions, but cannot make the socket a server-side single-channel subscription. Other-channel events are discarded using `broadcast.channel_id` before parsing `data.post`; an accepted `posted` must also have the same `post.channel_id`. Raw messages and nested post JSON are limited to 64 KiB. Bodies/author/team/attachments stay transient. Only `channel_id`, `post_id`, `created_ms` enter the persistent event queue or callback. Old, system, deleted and non-posted events are ignored.

At most three connection attempts (initial plus two reconnects) occur, with 1s/2s stop-aware backoff. All connections share one deadline, 1000-message receive budget, and five unique-event budget. Duplicate post IDs survive reconnects; the downstream stable event ID and queue dedup remain unchanged. A disconnect sets `possible_gap=true`, including an unsuccessful open. No REST fallback, replay parameters, history expansion or complete-delivery claim is made. Auth/redirect/TLS/protocol/malformed/oversize failures stop safely. Idle receives service callback retry timers. Once five unique posts have been accepted, further posts are ignored while the finite window allows pending deliveries to drain.

Cancellation, EOF, revocation, shutdown and expiry end reception and clear callback/signing state. A process restart does not resume a started trial: it disables the source and clears protected callback values without decrypting them. It does not offer durable source recovery. Delivered metadata may remain in the local runtime DB.

Event IDs and the signed HTTPS downstream are unchanged. Delivery is at-least-once; receivers must validate signatures and deduplicate stable event IDs. The original private owner/SID/hash supervisor, credential backend, tunnel configuration and private history are excluded. The official Mattermost MCP server is a separate product; equivalent event support has not been assessed.
