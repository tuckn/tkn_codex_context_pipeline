# Tkn Codex Context Pipeline

Codex appのProject状態と`~/.codex/sessions`を読み、chatを再利用可能な
Session Note v2へ変換し、そこからdurableなDecision Record v2を生成する、
独立したローカルデータパイプラインです。
Project folderにはmarker・設定・contextを一切書きません。

## 必要なもの

- Python 3.11以上
- [uv](https://docs.astral.sh/uv/)
- Session NoteまたはDecision Record生成時に`PATH`から実行できる`codex`
  - Windowsの場合、次のコマンドでインストールします。`powershell -ExecutionPolicy Bypass -c "irm https://chatgpt.com/codex/install.ps1 | iex"`

## インストール

次のコマンドでインストールします。例示している`C:\path\to\tkn_codex_context_pipeline`は、このリポジトリの実際のフォルダパスに置き換えてください。

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline"
tkn-codex-context --help
```

2つ目のコマンドは、インストール後に`tkn-codex-context`を実行できることを確認します。この方式では、インストール時点のコードが使用され、その後のリポジトリの変更は自動的に反映されません。

`git pull`などでリポジトリを更新するたびに、更新後のコードと依存モジュールをインストール済みのコマンドへ反映するため、次のコマンドで再インストールしてください。

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline" --force
tkn-codex-context --help
```

開発時には、代わりにeditable installationを使用できます。

```console
uv tool install -e "C:\path\to\tkn_codex_context_pipeline" --force
```

`-e`（`--editable`）を指定すると、インストールされたコマンドはリポジトリ内のソースコードを直接参照するため、ソースコードの変更は再インストールせずに反映されます。ただし、更新によって`pyproject.toml`または`uv.lock`の依存モジュールが追加・変更された場合は、tool環境にも反映するため、同じeditable installationのコマンドを`--force`付きで再実行してください。

## 設定

最初にCodex app Projectと保存先をdry-runで確認し、pipelineを初期化します。

```powershell
tkn-codex-context init --dry-run
tkn-codex-context init
tkn-codex-context config show
```

`init`はグローバル設定とProject registryを作成し、Codex app左ペインの
Projectごとに空の`sessions/`と`decisions/`を用意します。Session Noteや
Decision Recordは生成しません。
実行日時は`installed_at`として保存され、通常実行が
自動処理するのは、この日時以後に作成または更新されたchatだけです。それ以前の
chatは、明示的な`pull --backfill`または`rebuild`で処理します。

既存のpipelineを完全に作り直す場合は、まず削除対象を確認してからforce初期化
します。modelや保存先などの設定は維持され、`installed_at`だけが更新されます。

```powershell
tkn-codex-context init --force --dry-run
tkn-codex-context init --force
```

Project名や現在のrootから内部IDを確認する場合は、登録済みProjectを一覧表示します。

```powershell
tkn-codex-context projects list
tkn-codex-context projects list --json
```

標準出力は、状態、Project名、内部Project ID、現在のrootを含む人向けの表です。
`--json`では、script利用や詳細確認のために登録済みroot metadataも出力します。

アプリ自身のファイルは、用途別に次の場所へ保存します。

```text
~/.tkn/codex_context_pipeline/
├── config.yaml
├── data/
│   ├── project-registry.jsonl
│   └── projects/
│       └── <projectId>/
│           ├── sessions/
│           └── decisions/
└── state/
    ├── projects/
    │   └── <projectId>/
    │       ├── chat-refresh-state.json
    │       └── decision-build-state.json
    └── reports/

~/.cache/codex_context_pipeline/
└── 中断した処理を再開するためのcache
```

`data/`はProject registry、Session Note、Decision Recordなどの永続データ、`state/`はrefresh
checkpointやrun reportなどの再作成可能な状態・履歴に使用します。
cacheは既定で`~/.cache`へ分離し、`XDG_CACHE_HOME`が設定されている環境では
その場所を使用します。実行中だけ必要なmodel入出力などは、Pythonの標準一時
directoryを使うため、Windowsでは通常`%TMP%`、Linuxでは通常`/tmp`に置かれます。

設定の優先順位は次のとおりです。

1. built-in defaults
2. `~/.tkn/codex_context_pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. `--config`
5. CLI option

commitするのは`.tkn/config.example.yaml`だけです。実設定をcommitしないで
ください。YAML中の相対パスは、そのYAMLファイルがあるfolderを基準に解決します。
Session NoteとDecision Recordの生成profileはapplication-ownedで、ユーザー向け
設定keyはありません。旧設定の
`summary_prompt: null`は読み飛ばします。値を設定した`summary_prompt`が残っている
場合は、現在のCLIを実行する前にそのkeyを削除してください。

## 通常実行

### Project metadataを取得する

初期化後にCodex app Projectが追加・変更された場合は、Codex appからProject
metadataを取得します。これはCodex appからlocal registryへの一方向の更新です。
dry-runではregistryやProject directoryを変更しません。

```powershell
tkn-codex-context projects fetch --dry-run
```

確認後に反映します。

```powershell
tkn-codex-context projects fetch
```

### Project fetch結果の読み方

`projects fetch`および`session-notes pull`の`projectFetch.projects`には、
現在Codex appに存在するProjectごとに次の情報が出力されます。

| field | 意味 |
| --- | --- |
| `sourceProjectId` | Codex appから読み取った内部Project ID |
| `projectId` | registryと保存先folderで使用するProject ID。現在の仕様では`sourceProjectId`と常に同じ |
| `name` | Codex appに表示されている現在のProject名 |
| `status` | 今回のfetchでCodex app Projectとregistry recordを対応付けられたか |
| `method` | どの方法でregistry recordを決定したか |
| `roots` | Codex appに現在登録されている有効なroot。先頭がPrimary、以降がSecondary |

`projectFetch.projects[*].status`が現在取り得る値は次のとおりです。

- `bound`: Codex appの内部IDとregistryの`projectId`を対応付けられた状態。
  既存recordを再利用した場合と、新規recordを作成した場合の両方で使用します。

Codex appから消えたProjectは`projectFetch.projects`には含まれません。保存済み
Projectも含めた状態は`projects list`で確認します。こちらの`status`は次の意味です。

- `active`: 直近のfetch時点で、同じ内部IDのProjectがCodex appに存在する。
- `inactive`: Codex appからProjectが消えている。registry、Session Note、stateは
  削除されず、同じIDが戻れば次回fetchで`active`へ戻る。
- `unknown`: registry recordにstatusがない非標準状態。通常の`init`または
  `projects fetch`が作成したrecordでは発生しない。

`method`が取り得る値は次のとおりです。

- `project-id`: 同じ内部Project IDの既存registry recordを見つけ、そのrecordの
  名前とroot metadataを更新した。
- `new`: 同じ内部Project IDのrecordがなく、新しいregistry recordを作成した。
  `--dry-run`の場合は作成予定であり、まだ書き込んでいない。

`projects list --json`の`roots[*].status`では、現在のrootを`active`、以前のrootを
帰属判定用に保持したaliasを`historical`と表示します。

### Session Noteを生成する

`session-notes pull`は、通常処理の対象となるCodex chatを取り込み、Session Noteを
作成または更新します。実行前にProject metadataのfetchも自動的に行うため、
既定のJSON出力は簡潔です。`projectFetchSummary`と`reportSummary`に真偽値と件数を
表示し、`reportPath`で保存済みの完全なrun reportを示します。Project・thread単位の
詳細をstdoutにも表示したい場合だけ`--full-output`を使います。
生成ノートのFrontmatterは`type: summary`です。Project context layoutとの互換性の
ため、directory名とcommand名は引き続き`sessions`と`session-notes`を使用します。

最初にdry-runで対象を確認します。dry-runは生成AIを呼び出さず、registry、
Session Note、refresh state、cache、run reportのいずれも変更しません。

```powershell
tkn-codex-context session-notes pull --dry-run
```

既定の簡潔な出力に含まれる主なfieldは次の意味です。

| field | 意味 |
| --- | --- |
| `ok` | threadの失敗および未解決のProject bindingなしで完了したか |
| `reportPath` | 保存したrun report。dry-runでは保存しないため`null` |
| `projectFetchSummary.projectCount` | Codex appから取得したProject数 |
| `projectFetchSummary.boundCount` | 使用可能なlocal rootを紐付け済みのProject数 |
| `projectFetchSummary.newCount` | 新しく検出したProject数 |
| `projectFetchSummary.pendingCount` | rootの紐付けが必要なProject数 |
| `reportSummary.mode` | 通常pullは`daily`、過去分は`backfill`、再構築は`rebuild` |
| `reportSummary.selectedCount` | `--limit`適用後、作成・更新予定のSession Note件数 |
| `reportSummary.processedCount` | 作成・更新に成功したSession Note件数 |
| `reportSummary.failedCount` | 失敗したthread数 |
| `reportSummary.deferredCount` | runtime上限により次回へ延期したthread数 |
| `reportSummary.warningCount` | run warningの件数 |
| `reportSummary.scan.*` | file数、候補数、変更なし、対象外などの数値counter |

既定出力では、大きくなりやすい`projects`、`selected`、`processed`、error詳細の
配列を省略します。通常実行の詳細は`reportPath`のfileを確認してください。
dry-runはreportを保存しないため、選択されたProject、thread、sourceの詳細を
確認するときは`--full-output`を使います。

```powershell
tkn-codex-context session-notes pull --dry-run --full-output
```

dry-runで`reportSummary.selectedCount: 0`なら、今回作成・更新するSession Noteは
ありません。`reportPath: null`と`reportSummary.processedCount: 0`はdry-runの
通常動作です。

`reportSummary.scan.ignoredFiles`は対象外fileの合計であり、後続の個別counterは完全な内訳では
ありません。日時範囲またはidle条件で早期に除外されたfileは、
`ignoredFiles`だけが増えます。そのため`ignoredFiles`が全file数と同じで、
他の除外counterが0でもエラーではありません。

確認後に生成します。

```powershell
tkn-codex-context session-notes pull
```

通常のpullは30分以上idleのchatだけを処理します。過去分は明示的に実行します。

#### 既存Session Noteの更新判定

既存Session Noteについてsource fingerprint、artifact schema、model、
reasoning effort、要約prompt、出力schema、Markdown template、generator prompt
envelope、renderer versionが現在の条件とすべて一致する場合は
`scan.unchanged`としてスキップします。生成AIは呼び出さず、Session Noteとstateも
変更しません。

sourceが更新された場合、現在より古いschemaの場合、またはmodelなどの生成条件が
異なる場合は、自動的に作成・更新候補になります。現在より新しい未対応schemaは、
誤って上書きせずエラーで停止します。

条件が同じSession Noteも再生成する場合は`--force`を指定します。

```powershell
tkn-codex-context session-notes pull --force --dry-run
tkn-codex-context session-notes pull --force
```

通常の`pull --force`が対象にするのは`installed_at`以後のchatです。全履歴を
強制再生成する場合は、過去分と通常分をそれぞれ実行します。

```powershell
tkn-codex-context session-notes pull --backfill --all --force --dry-run
tkn-codex-context session-notes pull --backfill --all --force
tkn-codex-context session-notes pull --force
```

#### 過去chatをbackfillする

`pull --backfill`は、`installed_at`より前のchatを取り込みます。通常のpullと
同じfingerprint・schema・model判定を使用するため、変更のない最新ノートは
スキップします。dry-runでは生成AIも書き込みも発生しません。

```powershell
tkn-codex-context session-notes pull --backfill --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context session-notes pull --backfill --all --dry-run
tkn-codex-context session-notes pull --backfill --all
```

`--backfill`には、誤って全履歴を処理しないよう`--project-id`または`--all`が
必要です。`--project-id`と`--all`は`--backfill`なしでは使用できません。
`--project-id`には内部Project IDのほか、現在のProject NameまたはCURRENT ROOTを
指定できます。解決順は内部IDの完全一致、現在のNameの完全一致、CURRENT ROOTの
順です。root比較ではWindows pathの大文字小文字、`/`と`\`、末尾separatorの違いを
正規化します。NameまたはCURRENT ROOTで一致する有効なProjectが複数ある場合は、
誤選択せず一致したProject IDを表示してエラーで停止します。

#### 1つのProjectをrebuildする

`rebuild`は、1つのProjectに帰属する全chatを`installed_at`やidle時間に関係なく
再評価し、Session Note directoryとrefresh stateを整合した状態へ再構築します。
生成と検証がすべて成功してから新しい構成へ切り替えるため、途中失敗時は既存の
Session Noteとstateを維持します。

現在より古いすべての数値schema versionは再生成対象です。最新schemaかつsourceと
生成条件が同じノートは再利用します。現在より新しいschemaは未対応形式として
停止します。`--force`を付けると、最新のノートも含めて全対象を再生成します。

```powershell
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot>
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot> --force
```

#### Session Noteをvalidateする

`validate`は、指定した1つのSession Noteについて、現在のschema、必須Frontmatter、
source thread/ref、source fingerprint、必須見出し、本文とFrontmatterのstatus一致を
検証します。ファイルを変更せず、生成AIも呼び出しません。

```powershell
tkn-codex-context validate <session-note.md>
```

### Decision Recordを生成する

`decisions build`は、1つのProjectに保存されたSession Note v2を一次入力として
durableなdecisionを抽出します。通常経路では元のCodex chatを読み直しません。
`Explicit Decision`を持つ複数のSession Noteをbounded synthesis batchとしてモデルへ
まとめて渡します。生成単位はSession Noteではなくcentral decisionです。複数のNoteが
同じ判断の成立・修正・検証を示す場合は、`sourceSessionRefs`に根拠をまとめた1つの
Decision Recordを生成します。既存Decision Recordのindexも渡し、同じdecisionなら
既存IDを参照します。

最初に書き込みなしの計画を確認します。`decisions build`は既定でread-onlyです。
生成AIを呼び出さず、registry、Session Note、Decision Record、state、run reportを
変更しません。

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot>
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --full-output
```

`reportSummary.selectedCount`は生成対象のSession Note数、`synthesisBatchCount`はmodelへ
渡すbatch数、`createdCount`は新規record数、`updatedCount`は未review recordの
再構成数、`referencedExistingCount`は既存recordへ紐付けた件数です。dry-runで個別のSession Noteを
確認する場合は`--full-output`を使用します。

確認後、`--write`を明示して生成・保存します。

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --write
```

新規recordは`data/projects/<projectId>/decisions/DR-NNNN-<slug>.md`に保存します。
各recordは`Context`、`Decision`、`Rationale`、`Consequences`、`Applicability`、
`Verification`、`Materialization`、`Supersession`を持ちます。明示的なuser acceptance
または成立済みのoperational practiceが確認できる場合だけ`Accepted`とし、それ以外は
`Proposed`にします。statusと実装・検証状態は別fieldで管理します。検証できなかった
事項は`Verification`の`Limitations`に残し、format検証の成功と事実確認を区別します。

Decision生成は入力Session Noteを変更しません。生成依存はDecision Record側の
`sourceSessionRefs`に保持し、Session Noteごとの処理済み状態と逆引き`decisionIds`は
`decision-build-state.json`へ記録します。run reportの`decisionRefs`でも今回の対応を
確認できます。生成結果がno-actionの場合もstateだけに記録するため、同じSession Noteを
working contextなど別の下流処理で利用できます。

source hashとdecision生成profileが同じSession Noteは次回`unchanged`としてスキップします。
再評価する場合は、書き込みを明示したうえで`--force`を指定します。同一decisionは既存IDへ
紐付けます。`reviewStatus: unreviewed`のCodex生成recordは、複数Noteから得た訂正や重要な
追加根拠がある場合、IDと初回dateを保って再構成できます。review済みrecordのcentral
judgmentは自動更新せず、新しい`sourceSessionRefs`と`Related Evidence`だけを追記します。

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --write --force
```

生成したDecision Record v2は単独でvalidateできます。

```powershell
tkn-codex-context decisions validate <decision-record.md>
```

`projects list`の既定出力を除き、各コマンドはJSON結果を出力します。
機械可読な一覧には`projects list --json`を使います。Session NoteとDecision Recordの
commandは既定で簡潔な要約を出力し、通常実行の完全なreportは`reportPath`へ保存します。
完全なreport JSONもstdoutへ出す場合は`--full-output`を追加します。

進捗ログは既定でstderrへ、最終JSONはstdoutへ出力します。そのため、対話実行では
`[INFO] Starting thread 1/7: ...`や`[SUCCESS] Completed thread 1/7: ...`の
ような進捗を確認でき、scriptではstdoutだけを
安全にpipeまたはcaptureできます。実装にはPython標準の`logging` moduleだけを使い、
logging専用の外部dependencyは追加していません。

ANSI対応のinteractive terminalでは`[SUCCESS]`を緑、`[ERROR]`と`[CRITICAL]`を
赤で表示します。redirect、`NO_COLOR`、`TERM=dumb`では色を付けません。Windowsでは
consoleが対応している場合にvirtual-terminal processingを有効化します。

- `-q` / `--quiet`: 進捗を省略し、errorだけを表示
- `-v` / `--verbose`: `[DEBUG]`の詳細な診断情報と元の進捗eventを追加表示

```powershell
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot>
tkn-codex-context -q session-notes rebuild --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context -v session-notes pull
```

## 対象範囲

現在はSession NoteとDecision Recordの生成を扱います。current working contextと
global contextはまだ対象外です。

Codex app ProjectのPrimary rootとSecondary rootは、どちらも現在有効なrootとして
扱います。Secondaryをhistorical rootとはみなしません。複数rootが異なるGit
repositoryでも問題ありません。

## Projectとthreadの同定

保存先とreportの`projectId`には、Codex appの`local-projects`に保存された内部
Project IDをそのまま使用します。`--project-id`でNameまたはCURRENT ROOTを
指定した場合も、それらはCLI入力時の検索にだけ使われ、保存時には解決後の内部IDを
使用します。Project名とrootは変更可能なmetadataであり、Projectの
同一性判定には使用しません。同じrootでも左ペイン上で別Projectなら別Project、
同じ内部IDなら名前やrootが変わっても同じProjectとして扱います。

threadは、Codex appの明示assignment、全active rootに対する一意なcwd一致、
保存済みhistorical aliasの順で同定します。projectlessまたはambiguousなthreadは
除外してreportへ残します。

## Codex内部形式への依存

`.codex-global-state.json`とJSONL sessionはCodex appのprivateな内部形式であり、
公開された互換contractではありません。adapterは必要な構造を厳密に検証し、
破損・欠落・互換性のない変更を検出した場合は推測せずfail closedします。
元JSONLは常にread-onlyです。

生成にはephemeral・read-only sandbox・structured outputのCodex CLIを使います。
source/generator fingerprintが同じthreadはモデルを呼ばずno-opにします。noteと
refresh stateは検証後にatomic反映し、通常のpullとrebuildの中断済み生成物は再開に
利用します。

## 開発

### Application-ownedな生成profile

Session NoteとDecision Recordの生成resourceはapplication-ownedな開発者向けassetです。ユーザーはprompt、
schema、template、profileの選択・上書きを行えません。現在は1つのprofileをbundle
として読み込み、将来、開発者が別の要約patternを追加するときは同階層へprofile
directoryを追加します。

```text
src/tkn_codex_context/profiles/
├── summary/
│   └── default/
│       ├── prompt.md
│       ├── output.schema.json
│       └── template.md
└── decision/
    └── default/
        ├── prompt.md
        ├── output.schema.json
        └── template.md
```

| resource | 役割 |
| --- | --- |
| `prompt.md` | version付きの編集方針、各fieldの意味、development label、source・merge・repair modeの指示 |
| `output.schema.json` | Codex structured outputとPython検証が共用する、生成JSONのfield・型・enum・上限 |
| `template.md` | version付きの決定的なMarkdown見出し順序とsection配置 |

3つは次の生成pipelineとして連携します。

```text
source event + prompt + output schema
    -> 検証済みの中間JSON
    -> Python renderer + template
    -> Session Note

Session Note + existing decision index + prompt + output schema
    -> 検証済みのdecision中間JSON
    -> Python renderer + template
    -> Decision Record
```

output schemaは完成したMarkdownではなく、生成AIが返す中間JSONの契約です。
最終Frontmatter、必須見出し、event IDの整合性、`schemaVersion`はPythonが検証する
application contractです。bundle loaderはstrict schemaとtemplate placeholderを
検証しますが、fieldの意味を変える場合は開発者が関連箇所をそろえて変更します。

| 変更内容 | 通常変更するもの |
| --- | --- |
| fieldを変えない編集方針の変更 | promptとその`version` |
| 既存fieldの上限やenum変更 | schema、説明している場合はprompt、test |
| 生成fieldの追加・削除・改名 | schema、prompt、Pythonの検証・renderer、test。配置も変わる場合はtemplate |
| Markdown sectionの順序・見出し変更 | templateとその`version`。必須見出し・placeholder変更時はPython検証・testも変更 |
| Frontmatterや互換性のないSession Note形式の変更 | Python renderer・検証・test。通常は`SESSION_SCHEMA_VERSION`も更新 |

schemaはSHA-256で識別し、promptとtemplateは明示的なversionも持ちます。3つのhashは
生成fingerprintへ含め、provenanceは`config show`と生成ノートmetadataで確認できます。

開発用dependencyを同期して、test・静的検査・buildを実行します。

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```
