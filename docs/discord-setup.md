# Discord Botを本体へ接続する

本体とBotを同じリポジトリの別プロセスで動かします。通常テストは実Discordへ接続しません。
実Discordでコマンド登録、公開HTTPS経由の案件照会、本人向けカード表示を確認済みです。
**自動配信はまだです。** 通知取込・配送コードは実装済みですが、実Discordでの自動配送の
運用開始・動作確認は行っていません。確認ボタンからの承認・差し戻しも実機では未検証です。
以下の起動手順には、次段階で行う自動配信の手順も含みます。

資格情報なしの通し試験は、リポジトリルートで次を実行します。
本体・BotのHTTPアプリ、承認、合成実行、独立確認、通知配送をメモリ内の接続で検証します。

```powershell
python scripts/dev.py run --locked python scripts/check_discord_integration.py
```

## 1. Discord側を準備する

1. [Developer Portal](https://discord.com/developers/applications)でアプリを作成。
2. Application ID、Public Key、Bot Tokenを控える。TokenはチャットやGitへ貼らない。
3. 検証用サーバーへBotをインストールする。`bot` / `applications.commands` と、
   対象チャンネルの閲覧・メッセージ送信・埋め込みリンクの権限を設定する。
4. Discordの開発者モードからサーバーID、チャンネルID、操作する本人のユーザーIDを取得する。

HTTP Interaction方式を使用します。メッセージ内容の読み取りやGateway接続は不要です。
[公式: Interactions](https://docs.discord.com/developers/interactions/overview)

## 2. 本体の接続設定を作る

所有者が `.local/discord.json` を作成します。以下のID・秘密・サービスIDを実際の値へ置き換えます。
`bridge_token` は十分な長さのランダムな専用秘密（32文字以上）にします。
`users` のキーは**本人のDiscord User ID**、`actor` は本体 `tokens.json` に存在する主体です。

```json
{
  "application_id": "111111111111111111",
  "public_key": "REPLACE_WITH_64_HEX_PUBLIC_KEY",
  "guild_id": "222222222222222222",
  "bridge_token": "REPLACE_WITH_A_RANDOM_DEDICATED_SECRET",
  "routes": {
    "demo-checkout": {
      "channel_id": "333333333333333333",
      "disclose_plan_details": true
    }
  },
  "users": {
    "444444444444444444": {
      "actor": "reviewer",
      "service_ids": ["demo-checkout"]
    }
  }
}
```

`disclose_plan_details` は通常falseです。trueでは計画の操作パラメータ・確認条件をDiscordへ送ります。
例は合成デモ用です。実サービスは開示内容を確認して設定してください。
通知には案件ID、状態、確認待ち計画IDを載せ、ログ本文・原本・LLMの説明は載せません。

本体を停止・再起動できるタイミングで、ルートから起動します。

```powershell
$env:OPSYNE_DISCORD_CONFIG = (Resolve-Path .local/discord.json).Path
python scripts/dev.py run --locked opsyne serve
```

本体は `127.0.0.1:8765` のまま使用します。設定内容の変更は次の要求から反映されます。
環境変数による機能の有効化・無効化には本体再起動が必要です。

## 3. Botの設定を作る

`apps/discord_bot` で `.env.example` を `.env` にコピーし、各Discord設定を埋めます。
本体のログイントークンは使いません。追加する接続設定は以下です。

```dotenv
DISCORD_CONTROL_BRIDGE_URL=http://127.0.0.1:8765/api/discord/interactions
DISCORD_CONTROL_FEED_URL=http://127.0.0.1:8765/api/discord/notifications
DISCORD_CONTROL_BRIDGE_TOKEN=本体のbridge_tokenと同じ値
```

Bot独自の `DISCORD_INGEST_TOKEN` も32文字以上の別のランダム秘密で設定します。
`DISCORD_CHANNEL_IDS` は本体のroutesと対応させます。
環境構築は [Bot README](../apps/discord_bot/README.md) を参照してください。

## 4. コマンド登録と起動

以下はすべて `apps/discord_bot` で実行します。登録コマンドは実Discord APIへ接続します。

```powershell
python dev.py run --locked --env-file .env opsyne-discord register-commands
```

登録は対象guildの `opsyne` コマンドだけを作成・更新します。他のコマンド一覧を置換しません。
初期状態は管理者向けです。Discordのサーバー設定 → 連携サービスで、利用する担当者・チャンネルに
コマンドの利用権限を設定します。この設定だけではOpSyneの承認権限は付きません。
[公式: Application Commands](https://docs.discord.com/developers/interactions/application-commands)

別々のターミナルで3プロセスを起動します。

```powershell
# 1: Discordからの操作受付
python dev.py run --locked --env-file .env opsyne-discord serve

# 2: 本体の案件更新をBotの送信待ちへ保存
python dev.py run --locked --env-file .env opsyne-discord sync

# 3: 送信待ちを実Discordへ配送
python dev.py run --locked --env-file .env opsyne-discord worker
```

Botの待受は `127.0.0.1:8766`。HTTPSリバースプロキシ・トンネルを用意し、
外部へは **`/interactions` だけ**を転送します。`/notifications` と本体APIは公開しません。
Developer PortalのInteractions Endpoint URLに公開HTTPSの `/interactions` を設定します。
Discordの署名付きPINGで保存が成功することを確認します。

### 開発用の一時HTTPS URLを使う場合

Cloudflare Quick Tunnelではアカウントや独自ドメインなしで一時URLを作れます。
URLはトンネルを再起動すると変わるため、開発時の動作確認用です。
[公式手順](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)

Botの8766番を直接公開せず、署名付き操作だけを中継する入口を8767番で起動します。
この入口は `/notifications`、`/healthz`、本体APIを公開しません。Bot自体は8766番で起動しておきます。

```powershell
# apps/discord_botで、別のターミナルを使って起動
python dev.py run --locked python -m opsyne_discord.public_ingress
```

さらに別のターミナルで、公式cloudflared実行ファイルを使います。
この開発環境ではリポジトリ内の `.tools/cloudflared/cloudflared.exe` に配置しています。

```powershell
# リポジトリルートで起動
.\.tools\cloudflared\cloudflared.exe tunnel --url http://127.0.0.1:8767 --no-autoupdate
```

表示される `https://...trycloudflare.com` の末尾に `/interactions` を付け、
Developer PortalのGeneral Information → Interactions Endpoint URLへ貼り付けて保存します。
ブラウザでそのURLを開くだけでは動作確認できません。Discordが署名付きPOSTで確認します。
手動起動した入口とトンネルは、それぞれのターミナルでCtrl+Cを押すと停止します。

## 5. 合成デモで一周する

1. [本体のデモ手順](usage.md#2-apiキーなしでデモを一周する)に沿って案件・固定計画を作成。
2. Discordに案件状態と確認待ち計画IDが通知されることを確認。
3. 登録した本人で `/opsyne plan id:<計画ID>` を実行し、「計画を確認」を押す。
4. 本人だけに表示された計画を読み、120秒以内に「この固定計画を承認する」を押す。
5. 「Controlに承認を記録しました」と出る。本体で承認者と状態を確認。
6. 本体の操作担当者が実行し、その後に独立確認する。API成功だけで復旧済みにしない。

長い計画など全文を表示できない場合はボタンを出しません。本体画面で承認してください。
提案者本人・未登録の人・対象外サービス・古い確認ボタンは承認できません。
`/opsyne case id:<案件ID>` で現在の案件状態、`/opsyne plan` で計画状態・承認者を確認できます。

## 停止・復旧

- 各プロセスはCtrl+Cで停止します。起動・同期・登録コマンドのエラーは秘密や応答本文を表示しません。
- `sync` は通信・設定エラーで停止し、カーソルを維持します。原因を直して再起動します。
- 設定変更・権限世代変更・復元で同期が停止した場合、履歴を照合してから
  `reset-feed-cursor` を実行します。送信済みoutboxを削除しないでください。
- 導入以前の案件は更新時から通知されます。初回同期には導入後の過去イベントも含まれます。
- 送信UNKNOWNは自動再送しません。[Bot README](../apps/discord_bot/README.md)の照合手順に従います。
- 本体バックアップとは別に、Botの状態ディレクトリと秘密を含む接続設定を保管します。

公開HTTPS、実Discordでのコマンド・投稿、OSサービス化はローカルテストとは別に確認が必要です。
