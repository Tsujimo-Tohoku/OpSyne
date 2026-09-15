# OpSyne

観測、異常発見、調査、対応案、人間の承認、操作、独立した結果確認をつなぐ運用・SOC基盤。

**現在は、単一の管理者がローカルで運用する v0.1 の実装です。** ファイル／API経由のログ収集から、案件、調査、変換提案、承認、限定操作、結果確認までをWeb画面とAPIで扱えます。

## 起動する

Python + uv を採用しています。製品・検証環境は Python 3.12、uvの版は `.uv-version`、依存の解決結果は `uv.lock` に固定します。
セットアップ用の Python 3.11 以上と pip、初回のインターネット接続が必要です。

```text
python scripts/bootstrap.py
python scripts/dev.py run --locked python -m opsyne serve
```

セットアップは `.tools/` にuv、`.venv/` に開発環境を作ります。
Python 3.12 が見つからない場合は `.tools/python/` に取得します。
手元の Python を指定する場合は `python scripts/bootstrap.py --python <Python-3.12の実行ファイル>` を使います。
uv が既にPATHにあり指定版と一致する場合、`python scripts/dev.py` は `uv` に置き換えられます。
同期後は `python scripts/dev.py run --locked opsyne serve` でも起動できます。

[http://127.0.0.1:8765](http://127.0.0.1:8765) を開き、初回に生成される `.local/opsyne/tokens.json` の `owner` トークンで接続します。画面の「合成データで動作を確認」から、APIキーなしで承認・実行・独立確認を試せます。自己承認は禁止されるため、承認時は `reviewer` へ接続を切り替えます。

LLM調査と未知ログの変換提案には `OPENAI_API_KEY` が必要です。モデルはユーザー指定の `gpt-5.6-luna` です。`.env` は自動で読み込みません。設定方法、デモ、実サービス接続、バックアップ・復元は[利用手順](docs/usage.md)を参照してください。

## 実装した機能

- 原本・取得位置・送信待ちの永続化、重複排除、ファイル回転・切詰め・観測不足の記録。
- 版と対象を固定した宣言的な変換定義、未知状態、機械検知、証拠付き案件。
- 案件の証拠範囲を制限・マスクしたLLM調査と変換提案。提案は承認待ちとして保存。
- 別主体による固定計画の承認、実行直前の再照合、短命の署名付き許可、実行台帳、独立した確認。
- 合成デモと、管理者が登録するHTTP操作／GET確認。結果不明の操作は `UNKNOWN` のまま保持。
- ローカルダッシュボード、役割別トークン、監査記録、停止中のバックアップと復元後の操作保留。

SQLiteと同一プロセス内のワーカーを使う小規模構成です。複数ホストの障害対策、OS単位の資格情報隔離、包括的なSOC判断・侵害根絶、本番運用適合を保証する実装ではありません。HTTPの結果不明を、成功や再実行可能と読み替えません。

## 用意したもの

- [AGENTS.md](AGENTS.md): AIと人間が共有する開発ルール、設計上の不変条件、レビュー観点。
- [開発手順](docs/development.md): セットアップ、コマンド、依存更新、問題切り分け。
- [設計原文](docs/opsyne-design-architecture.md)と[出典](docs/design-source.md)。
- [責任と依存の境界](docs/architecture-boundaries.md)、[段階計画と受入条件](docs/development-plan.md)。
- [ADR](docs/adr/0001-development-foundation.md): 初期技術選定と未決事項。
- [ローカル製品の技術選定](docs/adr/0002-local-product.md): 保存、API、LLM、認可と配備上の制約。
- [初期検証記録](docs/setup-verification.md): 開発環境構築時の記録。現在の製品全体の検証結果とは区別します。
- [v0.1検証記録](docs/implementation-verification.md): 自動テスト160件、画面・配布物の確認と未確認範囲。
- Ruff、mypy、import-linter、pytest、GitHub Actions、Issue/PRテンプレート。

## 構成

```text
src/opsyne/    # 製品領域、保存、API、CLI、Web画面、起動時の結線
tests/        # 永続化・認可・失敗系・API・HTTP/LLM通信の模擬検証
scripts/      # setup / uv launcher / quality checks
docs/         # 設計原文、開発方針、段階計画、ADR
.github/      # CI と開発テンプレート
```

## 開発時の確認

```text
python scripts/dev.py run --locked python scripts/check.py
python scripts/dev.py build --no-build-isolation
```

通常テストは一時SQLiteと模擬HTTP応答で動き、実サービス・実LLM・APIキーを必要としません。実接続での性能や業務上の成功条件は、対象ごとに確認します。
