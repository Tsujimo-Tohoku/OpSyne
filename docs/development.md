# 開発手順

## 前提

- Git、セットアップ用Python 3.11以上、pip、初回のネットワーク接続。
- 製品・品質検査用Pythonは3.12（`>=3.12,<3.13`）。uvは `.uv-version` の指定版。
- 品質検査は一時SQLiteと合成データ、模擬HTTP応答で完結します。外部DBサーバー、Docker、実APIキーは不要です。

## セットアップ

リポジトリのルートで、Windows PowerShell / macOS / Linux 共通のコマンドを実行します。
環境によって `python` を `python3` に読み替えてください。

```text
python scripts/bootstrap.py
python scripts/dev.py run --locked python scripts/check.py
```

`bootstrap.py` は版の一致するuvがなければ `.tools/uv/` へ導入し、`uv sync --locked` を実行します。
ユーザー全体のPATHやPython設定を変更しません。既存の `.venv` 内の依存はlockに同期されます。
既存Pythonを使う場合は、実行ファイルを明示できます。

```powershell
python scripts/bootstrap.py --python 'C:\path\to\Python312\python.exe'
```

指定しない場合は `.python-version` の3.12をuvが検索し、不足時は `.tools/python/` に取得します。
明示した `UV_CACHE_DIR` / `UV_PYTHON_INSTALL_DIR` がある場合はその設定を尊重します。
仮想環境のactivateやPowerShell実行ポリシーの変更は不要です。

## 日常のコマンド

| 用途 | コマンド |
|---|---|
| ローカル製品の起動 | `python scripts/dev.py run --locked python -m opsyne serve` |
| 明示した環境ファイルで起動 | `python scripts/dev.py run --locked python -m opsyne serve --env-file .env` |
| 全品質検査 | `python scripts/dev.py run --locked python scripts/check.py` |
| テストのみ | `python scripts/dev.py run --locked pytest` |
| lint | `python scripts/dev.py run --locked ruff check .` |
| 整形 | `python scripts/dev.py run --locked ruff format .` |
| 型検査 | `python scripts/dev.py run --locked mypy` |
| 領域間の依存検査 | `python scripts/dev.py run --locked lint-imports` |
| lockとの整合確認 | `python scripts/dev.py lock --check` |
| 開発環境の同期 | `python scripts/dev.py sync --locked` |
| wheel / sdist作成 | `python scripts/dev.py build --no-build-isolation` |

PATH上に指定版uvがあれば `python scripts/dev.py` を `uv` に置き換えられます。
ビルドバックエンドもdev依存に含めてlockし、ビルドは同期済み環境で `--no-build-isolation` を使います。
初回のeditable installは分離ビルドになるため、`tool.uv.build-constraint-dependencies` にHatchlingとその依存の版も固定しています。
CIはWindowsとLinuxで同じ品質検査とビルドを実行します。CI定義の追加とGitHub上での実行成功は別です。

## 依存更新

```text
python scripts/dev.py add <package>
python scripts/dev.py add --dev <package>
python scripts/dev.py lock --upgrade-package <package>
python scripts/dev.py sync --locked
python scripts/dev.py run --locked python scripts/check.py
python scripts/dev.py build --no-build-isolation
```

`pyproject.toml` と `uv.lock` を一緒にレビューします。ライセンス、実際に使うAPI、境界への影響を確認します。
Hatchlingとその依存を更新する場合は、`tool.uv.build-constraint-dependencies` もlockの採用版に合わせて更新し、初回同期とビルドを確認します。
uvを更新する場合は `.uv-version` を変更し、`bootstrap.py` を再実行してlock・検査・ビルドを確認します。
Pythonの対応版変更は `.python-version`、`requires-python`、Ruff/mypy設定とCIを合わせて変更します。

## テストと設計の関係

pytestは、パッケージ利用に加え、重複・原本の不変性・ファイル回転・保存失敗・再起動・承認・期限・署名・実行の結果不明・独立検証・復元を確認します。OpenAI SDK 3の要求／応答は `httpx2.MockTransport` で検査します。HTTP Connectorの試験には `httpx.MockTransport` を使用します。模擬試験の成功は、実アカウントでのモデル利用可否や実サービスの復旧成功を意味しません。
import-linterは[責務境界](architecture-boundaries.md)の静的import規則を確認します。
これらは実行時の最小権限や永続化の正しさを保証しません。
機能を追加するときは[段階計画](development-plan.md)の受入条件をテストへ具体化します。
テストは `tmp_path` 等の隔離領域と合成データを使い、実データ・外部API・現在時刻に依存する試験を通常検査へ混ぜません。

## 作業の流れ

1. `git status --short`、AGENTS.md、設計の該当節を確認します。
2. 成果物と受入条件を小さく定めます。保存・認可・公開契約の決定はADRへ残します。
3. 実装と必要な失敗系テストを追加し、上記の品質検査を実行します。
4. PRテンプレートに振る舞い、検証結果、制約を記載します。

GitHubのリモート、branch protection、公開、merge、deployは今回設定していません。
リモート接続後にCI実行結果を確認し、運用に合わせて必須チェックとレビュー要件を設定します。

## ローカルファイル

`.venv/`、`.tools/`、`.uv-cache/`、`.local/`、`.env*`、DB、ログ、秘密鍵はGit対象から除外します。
[`.env.example`](../.env.example) に設定名と秘密を含まない例があります。`.env` の存在だけでは読み込みません。プロセスの環境変数か `serve --env-file .env` を使います。起動・役割・登録・バックアップ手順は[利用手順](usage.md)を参照してください。

`.local/opsyne/` には原本、4つのSQLite DB、署名鍵、ログイン用トークンが入ります。製品プロセスを停止して専用CLIで一式をバックアップします。DBだけのコピーや、復元先での過去操作の再実行は避けてください。

## 承認説明のブラウザ検証（任意）

`scripts/test_adapter_review_ui.cjs` は実ブラウザから作成・説明編集・競合・再確認・承認を試験します。
Node.js、Playwright、Microsoft Edgeが必要です。Playwrightが通常の探索パスにない場合は
`NODE_PATH` にインストール先の `node_modules` を指定します。通常のPython検査とは分離しています。

専用の空のデータディレクトリでテストサーバーを起動します。実運用のデータは指定しないでください。

```text
python scripts/dev.py run --locked python -c "from pathlib import Path; from opsyne.runtime import Runtime; from opsyne.api.app import create_app; import uvicorn; uvicorn.run(create_app(runtime=Runtime(Path('.local/issue4-ui-check')), background=False), host='127.0.0.1', port=8766)"
```

別の端末で実行します。

```text
node --check src/opsyne/web/app.js
node scripts/test_adapter_review_ui.cjs http://127.0.0.1:8766 .local/issue4-ui-check
```

テストは合成ログ・定義を作成します。保存・承認は実APIを使用し、Agent説明と旧形式の表示確認のみ
応答を合成データへ置換します。LLMは呼び出しません。デスクトップ・390px幅のスクリーンショットは
指定データディレクトリの `screenshots/` に保存します。終了後はテストサーバーを停止してください。

## 承認後の復旧処理のブラウザ検証（任意）

`scripts/test_recovery_ui.cjs` は、提案者とは別の管理者による承認→実行→独立確認を実APIで検証します。
合成対象の変更回数が1回で、独立確認PASS後に案件が解決することを確認します。
自己承認・権限不足・承認専用ユーザー・承認済み計画の実行・画面を閉じた場合も検証します。
通信途絶、競合、操作UNKNOWN、確認FAIL/UNKNOWNはブラウザの応答差し替えで試験します。

上記と同じNode.js、Playwright、Microsoft Edgeの環境で、**空の専用データディレクトリ**を使い、
バックグラウンド処理と実LLMを使わずにサーバーを起動します。使用済みデータでは再実行できません。

```text
python scripts/dev.py run --locked python -c "from pathlib import Path; from opsyne.api.app import create_app; import uvicorn; uvicorn.run(create_app(Path('.local/recovery-ui-check'), background=False), host='127.0.0.1', port=8769)"
node --check src/opsyne/web/app.js
node scripts/test_recovery_ui.cjs http://127.0.0.1:8769 .local/recovery-ui-check
```

サーバー起動後、別の端末でNode.jsのコマンドを実行します。スクリーンショットはデータディレクトリの
`screenshots/` に保存します。検証後は専用サーバーを停止します。実サービスの復旧や実LLMの品質を
検証するテストではありません。

## 問題切り分け

- `uv is missing` / 版不一致: `python scripts/bootstrap.py` を再実行。
- Python 3.11が選ばれる: `bootstrap.py --python <3.12実行ファイル>`。端末のPythonを置き換える必要はありません。
- ダウンロードが失敗: PyPI / Python配布元への接続・プロキシを確認。制限された実行環境ではネットワーク権限が必要です。
- lock不一致: 意図した依存変更なら `python scripts/dev.py lock` 後に差分を確認。通常検査で `--locked` を外して隠しません。
- VS Code: `.venv` のインタープリタを選択。Windowsでは `.venv\Scripts\python.exe`。
- Git未初期化の新規コピー: 作業先を確認して `git init -b main`。既存リポジトリには不要です。

## 公式資料

- [uv: locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)
- [uv: installation](https://docs.astral.sh/uv/getting-started/installation/)
- [OpenAI: AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

AGENTS.mdはリポジトリ直下に置き、日常の開発指針とします。新しいCodex作業でこのディレクトリを開いたときに読み込まれます。
