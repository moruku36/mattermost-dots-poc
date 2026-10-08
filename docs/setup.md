# Setup / 導入

## Offline reproduction (recommended first)

Use Python 3.11+ from the repository directory. No pip install is needed.

```sh
python -B -m unittest discover -s tests -v
python demo.py
```

Expect the synthetic demo to report `delivered=1`, `active_after_expiry=0`, `callback_values_after_expiry=0`, `network_calls=0`. It advances a fake clock across five minutes; it does not wait five real minutes. No private file is required. All callback fixtures use reserved example domains. Windows DPAPI checks use public dummy values, never a stored owner credential.

## Optional live Windows template

This path is provided for review/adaptation. It has **not been live-tested with ChatGPT after export**. The historical live trial used a different private owner supervisor. Do not claim the offline demo proves a live wake.

Requirements:

1. A Windows owner account with Python 3.11+, and one approved Mattermost channel containing synthetic test posts only.
2. A dedicated least-privilege Mattermost token. This code makes only a fixed-channel GET; token privileges must be restricted independently on the server.
3. A ChatGPT workspace/client supporting [MCP Events](https://developers.openai.com/plugins/build/mcp-events), an installed authorized plugin or MCP connection, and an authenticated tunnel/host capable of launching a local stdio server. These services and their configuration are **not bundled**. Follow their official setup instructions; do not expose this stdio service through an unauthenticated HTTP wrapper.
4. Explicit permission for source polling and outbound event delivery. Use only a receiver-generated callback URL/signing secret. Do not put either in source or a static example.

Set **only in your owner process** the names in [.env.example](../.env.example): `MM_CHANNEL_ID`, `MM_BASE_URL` (HTTPS origin), `MM_TOKEN` and `MM_TRIAL_APPROVED=YES`. The application does not read a `.env` file. Never paste a token into a chat, commit it, pass it as a command argument, or publish terminal logs. Any secret-manager/tunnel process must pass the same narrowly scoped environment to its child. Environment variables are not absolute protection from same-user/admin programs.

Prepare local session metadata, with no API or callback request:

```powershell
python -B public_runtime.py --prepare
```

This creates `runtime/run-owner-trial/session.json` under the ignored runtime directory. Preparation refuses if that directory already exists: inspect previous runtime state locally before authorizing a new trial. Runtime files contain channel/subscription metadata and protected callback state; never upload them.

Configure the authorized MCP host's child command as `python -B native_service.py`, working directory this repo. Pass `MM_NATIVE_RUN_DIR` as the absolute path to the newly prepared runtime directory. Use your actual path locally; no machine-specific path belongs in the repository. Do **not** type `events/subscribe` secrets into an interactive debug console or log stdio. The authenticated client supplies them programmatically.

The transport identity in this template is the constant `local-owner`; it is not OAuth, a Windows SID check, or multi-user authorization. Only launch it inside an already authenticated single-owner transport. The private supervisor's SID/pin/ACL checks are not part of this public template.

Ask the connected ChatGPT conversation to subscribe to `message.created` for the approved channel with a TTL of at most 300000 ms and an appropriate notification instruction. Discovery alone performs no source polling. Once the client verifies the callback and subscribes, the source polls every five seconds. During that bounded window, create one synthetic post **manually** in the approved channel. This service never creates a Mattermost post or reply.

Check the resulting conversation wake yourself. Do not publish actual post/channel IDs or callback data. At expiry or successful active unsubscribe, source polling stops and subscription callback/signing values are cleared. Then disconnect/stop the authorized MCP child to end the process and release its environment-held source token. Local runtime metadata may remain; dispose of it under your own data-retention policy.

## Important limits

- No auto-start, service installation, provider/key enrollment, tunnel launch, paid API use or package installation is performed by these commands.
- Up to 60 source reads and 5 unique events; 3 delivery attempts each. A source failure or possible full-page gap stops the monitor.
- Restart behavior exists only as experimental code and has not been verified as a reliable recovery guarantee. No always-on use, history replay, complete history or two-way conversation.
- Callback delivery is HTTPS-only, validates public DNS destinations at connection time, retains TLS hostname verification, and follows no redirects. The source HTTPS origin is explicitly owner-configured; it may be a private Mattermost server authorized by that owner.
- This repo's JSON-RPC errors/diagnostic files use fixed categories and counters. The surrounding host/tunnel still needs a logging policy that does not record credentials or callback params.

## 日本語での要点

まずofflineテストとdemoだけを実行してください。live用はWindows専用で、公開版のChatGPT再接続は未検証です。本人が許可した1チャンネル・専用token・認証済みMCP経路が必要です。環境変数名だけを例示しており、実値は本人プロセス内で設定します。元の端末固有のSID/hash監督部は含めていません。購読後の5分だけ合成投稿1件で確認し、終了後は接続プロセスも止めてください。自動返信・常時運転・双方向会話はありません。
