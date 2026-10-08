# Mattermost → OpenAI Dots：期限付き新着通知PoC

[English](README.md) · [構成](docs/architecture.md) · [導入手順](docs/setup.md)

**MattermostでOpenAI Dotsを使う**ための実験的なadd-onです。最初の一歩として、承認した1チャンネルを5秒ごとにRESTで読み、新着投稿のメタデータをMCP Eventsへ渡して、購読済みChatGPT会話を起こす経路を作りました。

現状は新着通知だけです。**双方向の会話・自動返信・Mattermostへの書込み・常時運転は未実装**です。Mattermost側はREST pollingであり、native pushではありません。HTTP応答に本文が一時的に含まれる可能性はありますが、イベントとして保存・配送するのは `channel_id`・`post_id`・`created_ms` の3項目だけです。

## 確認したこと

2026年10月8日の、非公開の監督付き実装による5分の試行について、本人は「新着1件、配送1件、対応するChatGPT会話のwake、REST読取り55回、期限後のactive subscription 0、署名情報の消去」を確認しています。元実装のオフラインテストは49 PASSでした。

**公開版を再度live接続した結果ではありません。** このrepoでは端末固有の監督部を公開用テンプレートに置換しました。公開版のテスト結果は[検証記録](docs/verification.md)に分けています。原本ログ・実際のID・callback URL・秘密値・個人SID・端末固有パス・private履歴は公開しません。

## 最初に試す

Python 3.11以上、標準ライブラリのみです。

```sh
python -B -m unittest discover -s tests -v
python demo.py
```

demoは架空ID・仮時計・一時DB・`.invalid`のcallback fixtureだけを使います。HTTP通信、Mattermost読取り、ChatGPT接続、投稿は実行しません。Windows以外ではDPAPI等のWindows専用テストをskipします。

live用テンプレートはWindows専用です。callback URLと署名情報の保管にDPAPIを使います。別途、本人が承認したMCP tunnel/hostと、MCP Eventsを利用できるChatGPT workspaceが必要です。tunnel client・pluginのインストール・公開hosting・workspace認証はこのrepoに含めていません。[導入手順](docs/setup.md)を参照してください。

## 範囲

- 1チャンネル・1購読・最大5分・読取り60回・unique event 5件・配送は各3回まで。
- discoveryだけではpollingしません。購読とcallback検証の成功後に開始します。
- 有効期限の延長、履歴の追加取得、完全なreplayは提供しません。新着だけでページ上限に達した場合はgapの可能性として停止します。
- 常時運転、再起動後の安全な継続、完全履歴、配送SLO、production readinessは保証しません。
- token自体の権限を狭くしてください。コードのチャンネル制限だけでは、tokenのサーバー側権限は狭まりません。
- 公開版は元のSID/hash付き監督部を含みません。本人プロセスの環境変数でtokenを受け取ります。認証なしで外部公開せず、秘密を環境ファイル・ログ・画面共有に出さないでください。同一ユーザーや管理者からの完全な隔離は保証しません。

Mattermostの[公式MCP server](https://docs.mattermost.com/administration-guide/configure/agents-admin-guide#mattermost-mcp-server)には既存のread/search/post等があります。このPoCはイベントで会話を起こす補完的な実験であり、公式serverに同等機能がないと主張しません。

[元のforum投稿](https://forum.mattermost.com/t/an-add-on-poc-to-bring-openai-dots-to-mattermost/26170) · [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events) · [MIT License](LICENSE)
