# Tkn Codex Context Pipeline

Codex appのProject状態と`~/.codex/sessions`を読み、chatを再利用可能な
Session Note v2へ変換する、独立したローカルデータパイプラインです。
Project folderにはmarker・設定・contextを一切書きません。

## 必要なもの

- Python 3.11以上
- [uv](https://docs.astral.sh/uv/)
- summary生成時に`PATH`から実行できる`codex`

## インストール

Python 3.11以上とuvを用意し、このrepositoryのrootでCLIをインストールします。

```powershell
uv tool install .
tkn-codex-context --help
```

## 設定

最初にCodex app Projectと保存先をdry-runで確認し、pipelineを初期化します。

```powershell
tkn-codex-context init --dry-run
tkn-codex-context init
tkn-codex-context config show
```

`init`はグローバル設定とProject registryを作成し、Codex app左ペインの
Projectごとに空の`sessions/`を用意します。Session Noteは生成しません。
実行日時は`installed_at`として保存され、通常実行が
自動処理するのは、この日時以後に作成または更新されたchatだけです。それ以前の
chatは、明示的な`backfill`または`rebuild`で処理します。

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
│           └── sessions/
└── state/
    ├── projects/
    │   └── <projectId>/
    │       └── chat-refresh-state.json
    └── reports/

~/.cache/codex_context_pipeline/
└── 中断した処理を再開するためのcache
```

`data/`はProject registryとSession Noteなどの永続データ、`state/`はrefresh
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
出力には`projectFetch`と`report`の両方が含まれます。

最初にdry-runで対象を確認します。dry-runは生成AIを呼び出さず、registry、
Session Note、refresh state、cache、run reportのいずれも変更しません。

```powershell
tkn-codex-context session-notes pull --dry-run
```

`report`の主なfieldは次の意味です。

| field | 意味 |
| --- | --- |
| `reportPath` | 保存したrun report。dry-runでは保存しないため`null` |
| `mode` | `daily`は通常のpull、`backfill`は過去chatの明示処理 |
| `scan.files` | 読み取ったCodex JSONL file数 |
| `scan.eligible` | fingerprint確認後に作成・更新候補となった件数 |
| `scan.unchanged` | 前回処理時からsourceと生成条件が変わらず、再生成しない件数 |
| `scan.staleGenerator` | sourceは同じでも生成条件が変わり、再生成候補となった件数 |
| `scan.ignoredFiles` | 日時範囲、idle条件、内部chat、帰属判定などの条件で対象外になったfile数 |
| `selectedCount` | `--limit`適用後、今回作成・更新する予定のSession Note件数 |
| `selected` | dry-runで選択されたProject、thread、sourceの一覧 |
| `processed` | 通常実行で作成・更新に成功したSession Note。dry-runでは常に空 |
| `failed` | 通常実行で処理に失敗したthread |
| `deferred` | runtime上限により次回へ延期したthread |

dry-runで`selectedCount: 0`かつ`selected: []`なら、今回作成・更新する
Session Noteはありません。`reportPath: null`と`processed: []`はdry-runの
通常動作であり、それ自体はエラーを意味しません。

`scan.ignoredFiles`は対象外fileの合計であり、後続の個別counterは完全な内訳では
ありません。日時範囲またはidle条件で早期に除外されたfileは、
`ignoredFiles`だけが増えます。そのため`ignoredFiles`が全file数と同じで、
他の除外counterが0でもエラーではありません。

確認後に生成します。

```powershell
tkn-codex-context session-notes pull
```

通常のpullは30分以上idleのchatだけを処理します。過去分は明示的に実行します。

```powershell
tkn-codex-context session-notes backfill --project-id <projectId> --dry-run
tkn-codex-context session-notes backfill --all
tkn-codex-context session-notes rebuild --project-id <projectId> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectId> --force
tkn-codex-context validate <session-note.md>
```

`projects list`の既定出力を除き、各コマンドはJSON結果を出力します。
機械可読な一覧には`projects list --json`を使います。`--verbose`は進捗ログを
追加し、`--quiet`はコマンド出力を維持したままログを抑えます。

## 対象範囲

初版はsession summary生成だけを扱います。decision、current working context、
global contextは対象外です。

Codex app ProjectのPrimary rootとSecondary rootは、どちらも現在有効なrootとして
扱います。Secondaryをhistorical rootとはみなしません。複数rootが異なるGit
repositoryでも問題ありません。

## Projectとthreadの同定

`projectId`には、Codex appの`local-projects`に保存された内部Project IDを
そのまま使用します。Project名とrootは変更可能なmetadataであり、Projectの
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

開発用dependencyを同期して、test・静的検査・buildを実行します。

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```
