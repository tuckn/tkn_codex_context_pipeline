# Tkn GenAI Chat Note Pipeline

English: [README.md](README.md)

AIとの会話を源泉データとして保存し、会話ごとに再利用可能なSession Noteを生成する
ローカルCLIです。依頼、訂正、失敗した試行、未解決の問い、根拠付きの時系列、
最後に確認できた状態を残し、後から異なる観点で考え直せるようにします。

**session** は時系列で連続した一連の会話を意味し、そのまとまりを一つのMarkdownノートに記録します。
GitHub CopilotやClaude Codeなどの製品用語に合わせた名称ではなく、session本来の意味に基づきます。

v0.11.0の処理はSession Noteで完了します。分類とWorking Contextは
[tkn_genai_context_curation_pipeline](https://github.com/tuckn/tkn_genai_context_curation_pipeline)、
Decision抽出は
[tkn_genai_insight_pipeline](https://github.com/tuckn/tkn_genai_insight_pipeline)の責務です。
各CLIは単独でインストールでき、バージョンを持つファイルで連携します。

## 使い方 — 最初の結果まで

### 必要なものとインストール

Python 3.11以上、uv、読み取り可能なローカルCodex JSONLログを用意します。
生成には設定済みの推論プロバイダーが必要です。既定はCodex CLIで、
Claude Code、GitHub Copilot CLI、ローカルOllamaも推論に利用できます。
**チャット取得元として実装済みなのはCodexだけです。**
推論プロバイダーを変えても、取得元の対応範囲は増えません。

~~~console
cd "C:\path\to\tkn_genai_chat_note_pipeline"
uv tool install .
tkn-genai-chat-note --help
tkn-genai-chat-note config init
~~~

表示された`~/.tkn/genai_chat_note_pipeline/config.yaml`を編集し、保存先と
利用可能なモデルを指定します。Codexを使う場合は、端末で`codex --version`と
`codex login status`を確認します。デスクトップアプリだけではCLIの代わりになりません。
[同梱設定例](src/tkn_genai_chat_note/resources/config.example.yaml)も参照してください。

### 最初の保存・生成

~~~console
tkn-genai-chat-note config show
tkn-genai-chat-note clone --dry-run
tkn-genai-chat-note clone
~~~

`clone`は未作成の管理領域を初期化し、取得できる全履歴を保存・正規化して、
対象のSession Noteを生成します。通常実行は書き込みを行い、履歴量に応じて
推論時間・トークンを消費します。

`--dry-run`はローカル入力を読み、実行条件と予定を検証します。
推論・ネットワークアクセスは行わず、ディレクトリ、ロック、cache、reportも作りません。

### 日常の更新と結果確認

~~~console
tkn-genai-chat-note pull
tkn-genai-chat-note status
tkn-genai-chat-note provenance validate
~~~

`pull`は追加・変更されたログを取得し、対象ノートの更新と未完了処理の再開を行います。
成功済みで入力条件が変わらなければ、モデルを再呼び出ししません。
既定の待機時間は30分です。活動中の会話の要約を延期しても、その前にRawを保存します。
`--limit 20`で1回の生成試行数を制限できます。

結果に表示されたノートとreportのパスを開いて確認します。
`status`は前回実行時の記録であり、現在の入力を再走査しません。
完了判定は対象Session Noteだけで行い、Scope・Decision・Working Contextの生成を待ちません。

## コマンド一覧

`--config`や推論設定などの共通オプションは、コマンドの前に置きます。

| コマンド | 動作 |
| --- | --- |
| `config init` | 同梱設定を作成。編集済み設定は保護し、`--force`時はバックアップ後に置換 |
| `config show` | 有効な設定、5段階の設定元、要約プロファイルのhashを表示 |
| `clone` | 初期化と全履歴の保存・生成。再実行で再開可能 |
| `pull` | 初期化済みの保存先へ差分を反映し、ノート生成を再開 |
| `raw ingest` | 推論せずに元のバイト列を保存 |
| `session-notes build` | ノートを更新。`--thread-id`で会話を選択 |
| `session-notes validate <artifact>` | ノートの読み取り専用検証 |
| `status` | 前回の対象範囲・状態・reportパスを表示 |
| `provenance validate` | hash、ID、来歴の関係を読み取り専用で検証 |
| `storage migrate` | 旧storage 2を新階層へコピー移行。`--dry-run`で対象ファイルを確認 |

生成コマンドでは`--dry-run`、`--force`、`--allow-edited`、`--full-output`を使えます。
`--force`は入力が同じでも再評価しますが、review済みファイルの保護は解除しません。
`--allow-edited`は手編集した未reviewノートの置換を明示的に許可します。
Raw取得は`--dry-run`と`--full-output`に対応します。

進捗は標準エラー、結果は標準出力のJSONです。`-q`は進捗を抑制し、`-v`は診断を追加します。
終了コードは、成功・計画検証が`0`、失敗が`1`、clone/pullの未完了が`2`です。
単独ノートのbuildでも、reportにはノート全体の処理状況が含まれます。

## 設定の詳細

優先順位は、組み込み既定値 → ユーザー設定 → 現在のフォルダの`.tkn/config.yaml` →
明示した`--config` → CLIオプションです。各設定ファイルを統合前に検証します。
相対パスはその値を宣言した設定ファイルの場所から解決します。
設定スキーマ4.0.0はsnake_caseのキーと引用したSemVerを使い、
未知のキーや未対応の新しいバージョンはエラーにします。

~~~console
tkn-genai-chat-note --config "C:\path\to\config.yaml" clone
tkn-genai-chat-note --idle-minutes 0 --runtime-minutes 60 pull --limit 20
~~~

`raw_root`、`data_root`、`state_root`、`cache_root`は相互に分離し、
元ログや設定ファイルとも重ならない場所にします。これらは全取得元に共通のrootです。
その下に`<provider>/<source_id>/`を自動作成します。
アプリのProject情報がなくても、会話の保存は進められます。

推論の接続設定・認証は[推論プロバイダー](reference/inference-providers_ja.md)を参照してください。
外部CLIによる生成では、選択した入力がそのサービスへ送られる場合があります。
Ollamaの接続先はループバックに限定します。利用可能なモデルと認証はプロバイダー側の管理です。


### Session Noteの言語

既存の`config.yaml`の`generation.session_note_profile`で、`default-jp`（日本語・既定）または`default-en`（英語）を選択します。以下は設定の抜粋です。他の設定は維持してください。

```yaml
generation:
  session_note_profile: default-en
```

1回だけ切り替える場合は、`tkn-genai-chat-note --session-note-profile default-en pull`を使います。オプションはコマンドの前に指定します。`config show`に選択したprofile、リソースとhash、設定元を表示します。

両profileのschema・見出し・時系列・引用・状態判定は共通です。本文の言語と説明文だけを切り替え、時刻はAsia/Tokyoを維持します。カスタムprofile名・フォルダ・promptの指定には対応しません。組み込みリソースは`profiles/default-jp/`と`profiles/default-en/`に配置しています。

言語を変更すると、次のbuild/pullで既存ノートが再生成の対象になります。1会話につき1ノートとそのIDを維持するため、言語別のノートは併存しません。レビュー済み・編集済みノートの保護は維持し、dry-runでは生成も書き込みも行いません。異なるprofileの途中生成結果は再利用しません。

### Chat取得元と生成AI

`chat.providers`は会話の取得元、`generation.providers`はノート生成に使うAIの設定です。
`--provider`は`generation.active_provider`だけを切り替え、取得元を変更しません。

| Chat provider | 既定のhome | enabled | 対応状況 |
| --- | --- | --- | --- |
| `codex` | `~/.codex` | `true` | ローカルログの取得に対応 |
| `claude-code` | `~/.claude` | `false` | 設定のみ。取得・正規化・ノート生成への接続は未実装 |
| `github-copilot` | `~/.copilot` | `false` | 設定のみ。取得・正規化・ノート生成への接続は未実装 |

各セクションに`enabled`、`home`、`source_id`を指定します。
`include_archived`はCodex専用です。無効の取得元のhomeは存在しなくても設定を読み込めます。
未実装の取得元を有効にすると、実行時はdry-runを含めて書き込み前にエラーになります。
全取得元を無効にした場合も実行を止めます。`config show`では設定を確認できます。

`source_id`はPC名だけでなく、環境・アカウント・取得元を区別する安定した識別子です。
新しい取得元には、例えば`pc-a-windows-main-codex`、`pc-a-wsl-work-codex`のように、
同じprovider内で取得環境を区別できる値を最初の取得前に決めてください。
英数字・ドット・アンダースコア・ハイフンを使用し、先頭は英数字にします。
取得元の識別単位は`(provider, source_id)`です。異なるproviderには同じsource_idを使えます。
同じprovider内では別環境に別IDを指定し、Windowsでは大文字小文字だけの違いで区別しないでください。
サンプルの`my-windows-pc`は初回取得前に自分の環境の識別子へ変更します。
取得後のID変更は保存先・来歴の移行を伴うため、単なるラベル変更として扱わないでください。

1つの設定ではproviderごとに1取得環境を指定します。実行対象は現在もCodexだけです。
環境別の設定ファイルから順に実行すると、同じroot内に複数のCodex取得元を保持できます。
同一providerの複数環境を1回で取得する機能と、Claude Code・Copilotの取得は未実装です。

### 旧設定からの移行

設定schemaは`"4.1.0"`です。既存の4.0.x設定も読み取れ、profile未指定時は日本語を使います。旧schema 3.0.xの`codex_home`、`source_id`、
`include_archived`は、読み込み時に`chat.providers.codex`配下へ移行します。
`codex_home`の新しいキー名は`home`です。schema 2.0.x〜2.2.xと整数`2`も、
`scopes`が空または未指定の場合は従来どおり読み取れます。
元ファイルは自動で書き換えず、`config show`に移行と設定元を表示します。

手動で移行する際はschemaとキーの配置を同時に変更し、`source_id`と既存の保存先を維持します。
新旧形式を混在させないでください。設定を保存する処理は新形式だけを書き出します。

## 保存構造と責務の境界

~~~mermaid
flowchart LR
    L["ローカルCodexログ"] --> R["Rawコピーとmanifest"]
    R --> E["Canonical Events"]
    E --> T["Session Note"]
    M["観測したProject所属"] --> C["会話catalog"]
    T --> C
    C --> U["Context分類CLI"]
    T --> I["洞察CLI"]
    R --> P["バージョン付き根拠"]
    E --> P
    T --> P
~~~

保存先は「領域の役割 → 取得元アプリ → 取得環境 → データの種類」の順です。
以下の`P`は取得provider、`I`はsource_id、`T`はthreadKey、`H`は内容hashです。
`generation.active_provider`を変更しても保存先は変わりません。

| 保存パス | 内容 |
| --- | --- |
| `<raw_root>/P/I/sessions/...` | Codex元ログの相対構造とバイト列を保持した最新コピー |
| `<raw_root>/P/I/archived_sessions/...` | Codex側のアーカイブ構造を保持した最新コピー |
| `<raw_root>/P/I/manifest.jsonl` | 取得元・参照・hashを記録するRaw manifest |
| `<raw_root>/P/I/metadata/H.json` | 観測したアプリのProject情報 |
| `<data_root>/P/I/source-aligned/T/H.json` | 元ログの参照位置を持つCanonical Events |
| `<data_root>/P/I/session-notes/YYYY/MM/...md` | 会話開始年月で分けた現在のSession Note |
| `<state_root>/P/I/pipeline.json` | 取得元ごとの初期化情報・storageバージョン |
| `<state_root>/P/I/threads/T/...` | 会話単位の内部checkpoint |
| `<state_root>/P/I/ledger.json`・`reports/`・`last-run.json`・`normalization/` | 取得元ごとの実行・正規化状態 |
| `<cache_root>/P/I/...` | 取得元ごとの再利用可能な生成作業cache |
| `<data_root>/catalog/threads.json` | 全取得元の会話・所属・状態・ノート参照をまとめた共通catalog |
| `<data_root>/provenance/...` | 全取得元で共有する不変snapshot・entity・activity・公開index |

例えば、providerが`codex`、source_idが`my-windows-pc`なら、Rawは
`~/.tkn/genai_chat_note_pipeline/raw/codex/my-windows-pc/sessions/...`、
ノートは`~/.tkn/genai_chat_note_pipeline/data/codex/my-windows-pc/session-notes/YYYY/MM/...md`です。
同じsource_idでも`claude-code`や`github-copilot`とは別のフォルダになります。
`config show`の`storage.sourceRoots`で各providerの実際の保存先を確認できます。

root直下には共通の所有権markerとロックも置きます。共通catalog・来歴の更新はrootのロックで直列化し、
別取得元の記録を残します。`status`は現在選択した取得元、`provenance validate`は共通来歴を対象にします。
同じ会話が複数環境に存在する場合、threadKeyは同じでも、ノートIDとcheckpointは環境ごとに独立します。
共通catalogの行は`(sourceProvider, sourceId, threadKey)`で区別します。

### 旧保存領域の移行


v0.11.0では成果物名をSession Note、コマンドと出力フォルダを`session-notes`へ変更しました。
新規ノートは`type: sessionNote`、`sessionNoteId`、schema 6を使います。
旧Thread Noteのschema 3～5は読み取り可能です。移行処理は旧ノートの内容を変えずコピーします。
その後の`pull`では対象の未reviewノートを新形式で再生成するため、推論を実行する場合があります。
review済み・手編集済みノートは既存の保護ルールに従います。
`threadId`や`sourceThreadIds`はCodex側の会話を識別するため維持します。
下流の読み取り側にはschema 6への対応が必要です。別アプリのcuration・insightとの連携は、
今回の名称変更について未検証です。既存CLIの更新には`uv tool install . --reinstall`を使います。

storageは`4`、設定schemaは引き続き`"4.1.0"`です。新規の保存先は通常の`clone`で初期化します。
旧storage 2/3や旧Rawだけの領域を検出した場合、通常処理は移行の案内を出して停止します。
`source_id`と4つのrootを旧領域に合わせてから、次の順に実行します。

~~~console
tkn-genai-chat-note config show
tkn-genai-chat-note storage migrate --dry-run
tkn-genai-chat-note storage migrate
tkn-genai-chat-note provenance validate
tkn-genai-chat-note pull --dry-run
~~~

`--dry-run`はコピー元・コピー先・サイズ・hashを表示し、ファイル・ロック・フォルダを作りません。
通常実行はコピーして検証し、管理用のcatalog・index・checkpoint参照を更新します。
ノート本文・ノートID・review状態・不変の来歴snapshotは書き換えず、推論も実行しません。
移行済みの再実行は変更しません。異なる内容のコピー先がある場合は停止します。
旧Project構造だけがあり、storage 2の管理情報がない領域は自動では移行しません。
Rawだけの移行では来歴indexがまだないため、`provenance validate`は最初のノート生成後に行います。

**旧Raw・ノート・正規化データも残します。** 既存ノートや過去の来歴が持つ旧参照を維持するためです。
新しい処理は新階層を使います。移行後に旧フォルダを手動で削除すると過去の参照を失う場合があります。
変更する共通管理ファイルの元データは
storage 2では`<state_root>/P/I/migrations/storage-v2-backup/`、
storage 3では`<state_root>/P/I/migrations/session-note-v4-backup/`に保存します。
既知の書き込みエラー時は変更したファイルを復元します。storage 2の移行では、旧版CLIによる再書き込みを防ぐため、
旧`<state_root>/pipeline.json`は共通の新レイアウトmarkerに置き換え、元ファイルをバックアップします。
旧領域がない場合の`storage migrate`は何も作成せず終了します。

Projectへの所属が変わっても会話のIDは変わりません。所属の観測は上流に残し、
意味に基づくScopeや承認済みの関連は下流で扱います。
Session Noteは派生した記録であり、元の根拠を置き換えません。
取得元と推論プロバイダーは別の概念です。

ローカルの`sessions`と、既定では`archived_sessions`を対象にします。
Project未所属・対応先不明・所属が曖昧な会話も対象です。
内部処理・承認レビューの会話や通常のユーザー発言を持たないログは、
保存・正規化しても要約からは除外します。クラウドだけにあるChatGPT/Work履歴は取得しません。
未対応のレコードや分岐して矛盾する履歴はreportに残します。

ID、hash、schema、引用、保存構造、入力準備の詳細は
[データ契約](reference/data-contract.md)、
[Session Noteの内容](reference/session-note-format_ja.md)、
[処理のシーケンス](reference/processing-flow_ja.md)を参照してください。

## 更新後の再インストール

コードやresourceを更新した後は再インストールします。

~~~console
cd "C:\path\to\tkn_genai_chat_note_pipeline"
uv tool install . --reinstall
~~~

## 開発と検証

~~~console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
~~~
