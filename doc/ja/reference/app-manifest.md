# アプリマニフェスト — `palmimo.toml`

Palmimo に搭載するアプリ（このリポジトリのサンプルのような、
`pyproject.toml` ベースの Python プロジェクト）は、ルートに置く 1 つの静的ファイル、
`palmimo.toml` で自身を宣言します。ホストはこのファイルだけを読み、アプリを
カタログに掲載し、パラメーターフォームを構築して起動します。これらの情報を得るために
アプリの Python コードをインポートしたり実行したりすることはありません。`schema` はマニフェスト自体の
バージョンを宣言します。将来、この形式に互換性のない変更を加える場合は値を上げます。

## 例

`examples/teleop/palmimo.toml` の全文です。

```toml
schema = 1
name = "teleop"
description = "Drive Palmimo from a phone or laptop browser over WebSocket, with live MJPEG camera video, on the same LAN."
command = ["palmimo-teleop", "--port", "{port}", "{no_camera}"]
url = "http://{host}:{port}/"
devices = ["camera", "motor_display"]

[params.port]
type = "int"
default = 8000
min = 1024
max = 65535
description = "HTTP/WebSocket port the teleop web app is served on."

[params.no_camera]
type = "bool"
default = false
flag = "--no-camera"
description = "Disable the head camera video feed."
```

## ファイル名

アプリのディレクトリには複数のマニフェストを含められます。ファイル名は `palmimo.toml`
（デフォルト）、または `palmimo.<variant>.toml` とし、`<variant>` は
`^[a-z][a-z0-9-]{0,39}$` に一致する必要があります。ディレクトリ内のそれ以外の名前（`palmimo_backup.toml`、
`palmimo.TOML`、`palmimo..toml`）はマニフェストとはみなされず、無視されます。各
マニフェストファイルは、それぞれ独自の `name` を持つ独立したアプリで、この名前は
カタログ内で一意です。アプリを名前で起動するホストは、マニフェストの指定が
なければ `palmimo.toml` を読みます。`examples/agents/companion/palmimo.realtime.toml` がその
例です。同じディレクトリの `palmimo.toml` はカスケード型パイプラインのアプリ、
`palmimo.realtime.toml` は OpenAI Realtime の音声ランタイムです。
この 2 つは必要な環境変数とデバイスが異なるためです。

## アプリの識別情報

デバイス上では、アプリは `<namespace>.<name>` 形式の ID で識別されます。
ホストはアプリのインストール元から名前空間を決定します。
公式カタログからのインストールでは `palmimo`、GitHub からは
リポジトリの所有者、zip アップロードでは `zip` などになります。
マニフェストで名前空間を設定することはありません。

`name` は ID の名前部分のデフォルト値であり、カタログ上のアプリの表示名に
すぎません。あらゆるインストール元を通して一意である
必要はありません。インストール時にデバイス上の既存アプリと衝突する場合、
ホストは名前部分に `-2`、`-3` などを付けます。また、
インストールする人は完了前に名前を変更できます。
名前空間がすでに組織やベンダーを表しているため、
`name` にそれらを含めないでください。`palmimo-` などの接頭辞は不要です。

## フィールド

| キー | 型 | 必須 | 規則 |
|---|---|---|---|
| `schema` | int | はい | 現在は `1` のみ受け付けます |
| `name` | string | はい | `^[a-z][a-z0-9-]{0,39}$` に一致する必要があります。用途は[アプリの識別情報](#アプリの識別情報)を参照してください |
| `description` | string | はい | 1〜200 文字。カタログの 1 行の概要です |
| `command` | 文字列の配列 | はい | 実行する argv です。シェル文字列ではなく配列なので、シェルが解析することはありません。各要素に 1 つ以上の `{param}` プレースホルダーを含められます |
| `url` | string | いいえ | アプリの実行開始後に開くページです。`{host}` とパラメーターのプレースホルダーを含められます。`http://` または `https://` で始まる必要があります（`javascript:` などは拒否されます） |
| `devices` | 文字列の配列 | いいえ | `camera`、`audio`、`motor_display` の部分集合です。省略した場合（または空の場合）、デバイスは一切開きません |
| `[env.<NAME>]` | テーブル | いいえ | アプリが読み取る環境変数ごとに 1 つのテーブルです。[環境変数](#環境変数)を参照してください |
| `[params.<name>]` | テーブル | いいえ | 起動時のパラメーターごとに 1 つのテーブルです。[パラメーター](#パラメーター)を参照してください |

この表にないトップレベルのキーは、すべて検証エラーになります。不明な
キーは、意図的に使わない拡張であるよりも、タイプミスにより何も起きないまま
見過ごされる可能性のほうがはるかに高いためです。

## 環境変数

`[env.<NAME>]` は、アプリが起動時に読み取る環境変数を 1 つ宣言します。
`<NAME>` は `^[A-Z][A-Z0-9_]{0,63}$` に一致する必要があります。`PALMIMO_` で始まる名前は、
ホスト自身が注入する変数（たとえば、アプリ自身の ID である `PALMIMO_APP_ID`。
[アプリの識別情報](#アプリの識別情報)を参照してください）のために予約されており、
ここでは宣言できません。

| キー | 型 | 必須 | 規則 |
|---|---|---|---|
| `required` | bool | いいえ（デフォルトは `true`） | アプリの実行にこの変数が必須かどうかです |
| `description` | string | はい | 1〜200 文字。キーの用途で、カタログでキーの隣に表示されます |
| `help_url` | string | いいえ | 値の取得先を示す `http://` または `https://` のページです（たとえば API キーの登録ページ） |

`required = false` の変数に値が割り当てられていない場合は、完全に
未設定のままになります。空文字列にはなりません。

## パラメーター

`[params.<name>]` は起動時のパラメーターを 1 つ宣言し、
`command` と `url` 内の `{name}` が現れる箇所に代入されます。`<name>` は
`^[a-z][a-z0-9_]{0,31}$` に一致する必要があります。`default` のないパラメーターは、
アプリを起動する前に入力が必要です。

| `type` | 追加のキー | 代入される値 |
|---|---|---|
| `string` | `default`、`description`（文字列）、`pattern`（制限付きの正規表現、`re.fullmatch`、最大 256 文字）、`max_length`（デフォルトは 256） | 値そのもの |
| `int` / `float` | `default`、`description`（文字列）、`min`、`max` | 値の十進表記 |
| `enum` | `choices`（必須、空でない文字列の配列）、`default`、`description`（文字列） | 選択された値 |
| `bool` | `default`、`description`（文字列）、`flag`（必須） | 下記を参照してください |

`bool` パラメーターには代入される値がありません。true の場合、
`{name}` と等しい `command`/`url` の要素全体を `flag` に置き換えます。
false の場合、その要素を完全に削除します。このため、`bool` パラメーターの `{name}` は、
それを含む要素の*全体*でなければなりません。`"--x={verbose}"` のように
より大きな文字列に混在させると検証エラーになります。他のすべての
パラメーター型では、他のテキストやプレースホルダーと同じ要素に含められます
（`"--addr={host}:{port}"` は問題ありません）。

プレースホルダー名のうち 2 つは予約されており、パラメーターとして宣言しません。`{host}`
（デバイスのホスト名）と `{app_dir}`（アプリ自身のディレクトリの
絶対パス）です。`command` または `url` で使うその他のプレースホルダーは、すべて
宣言済みのパラメーター名である必要があります。未定義のものは、不明なトップレベルの
キーと同様に検証エラーです。プレースホルダー名のタイプミスによって、
実際の値の代わりにリテラルの `{typo}` が気づかれないまま argv に渡されることを防ぎます。

## 検証規則

次のいずれかに当てはまると、マニフェストの検証は失敗し、
アプリの掲載も起動もできません。

- `schema` が `1` ではない
- 不明なトップレベルのキーがある
- `name` が指定のパターンに一致しない
- `command` が空である
- `command` または `url` 内のプレースホルダーが、予約済みのプレースホルダー名でも
  宣言済みのパラメーター名でもない。これには、名前がパラメーター自体の
  パターン（`^[a-z][a-z0-9_]{0,31}$`）にさえ一致しないもの、たとえば `{}` や
  `{foo-bar}` も含みます。そのような名前は宣言できないため、実際の名前をタイプミスした
  場合と同様に未定義です
- `devices` に `camera`、`audio`、`motor_display` 以外の値がある
- `[env.<NAME>]` テーブルに不明なキーがある（`required`、`description`、`help_url`
  以外のキー）
- `[env.<NAME>]` の名前が予約済みの `PALMIMO_` 接頭辞と衝突する
- `[env.<NAME>]` テーブルに `description` がない
- `[env.<NAME>]` の `required` が bool ではない、または `help_url` が
  `http://` もしくは `https://` の URL ではない
- `[params.<name>]` テーブルに、その `type` では認められないキーがある（たとえば
  `string` パラメーターの `choices` や `int` パラメーターの `flag`）
- パラメーターの `min`、`max`、`max_length` の型が誤っている（`min`/`max` は
  数値、`max_length` は正の int である必要があります）、または `choices` が
  空でない文字列の配列ではない
- パラメーターの `default` が、宣言された `type` に一致しない（`int`
  パラメーターの `default` は int である必要があり、たとえば文字列の `"8000"` は不可です。
  `float`、`enum`、`bool`、`string` も同様です）
- パラメーターの `default` が自身の制約を満たさない。`int`/`float` で
  `min`/`max` の範囲外、`enum` で `choices` に含まれない、`string` で
  `max_length` より長い、または `pattern` に一致しない場合です
- パラメーターの `description` が存在するが文字列ではない
- `string` パラメーターの `pattern` が 256 文字より長い、または次のもの以外を
  使用している。アンカー（`^`、`$`）、リテラル文字、エスケープ、文字
  クラス（`[...]`）、および 1 つの文字、エスケープ、文字クラスに適用する
  1 つの量指定子（`?`、`*`、`+`、`{m}`、`{m,n}`）です。グループ、選択、
  後方参照、先読み・後読みのアサーションはサポートしていません。
- `bool` パラメーターに `flag` がない
- `bool` パラメーターのプレースホルダーが、要素全体ではなく、
  より大きな要素の中に混在している

## 公式カタログ

サンプルのリリースには、公式アプリを掲載するホスト向けに生成された
カタログが含まれます。各エントリーの `source` は、アプリの Git リポジトリ、`subdir`、
マニフェスト（`palmimo.toml` 以外の場合）、リリースタグを示します。また、
カタログ生成時にリリースタグが指していたコミット SHA を `commit` として
含みます。ホストは公式アプリをインストールする前に、タグをチェックアウトした結果が
その SHA に一致することを確かめる必要があります。これにより、リリース後に
移動されたタグを拒否します。

## このリポジトリ内のファイル

| パス | 役割 |
|---|---|
| [examples/teleop/palmimo.toml](../../../examples/teleop/palmimo.toml) | 上記の例 |
| [examples/agents/companion/palmimo.toml](../../../examples/agents/companion/palmimo.toml) | コンパニオンエージェントのマニフェスト |
| [examples/agents/companion/palmimo.realtime.toml](../../../examples/agents/companion/palmimo.realtime.toml) | コンパニオンの OpenAI Realtime 音声ランタイム。同じディレクトリ内の別アプリです |
| [examples/agents/wakeword/palmimo.toml](../../../examples/agents/wakeword/palmimo.toml) | ウェイクワードエージェントのマニフェスト |
