# MCP サーバー

`palmimo_sdk.mcp` は、ウェイクワードのサンプルが使うものと同じエージェントツール（forward、wave、set_face、say、capture、...）を [Model Context Protocol](https://modelcontextprotocol.io/) 経由で公開します。 MCP に対応するクライアントであれば、Claude Code、Claude Desktop、その他の MCP エージェントから、直接ツールを一覧取得し、呼び出せます。MCP Python SDK 2.x を基に構築しており、接続するクライアントに合わせて、最新のプロトコルリビジョン（2026-07-28）と古いリビジョン（2024-11-05〜2025-11-25）のどちらもネゴシエーションできます。

## インストール

```bash
# From this workspace root
uv sync --extra mcp

# Or, when using `palmimo_sdk` standalone, outside this workspace
uv add "palmimo-sdk[mcp]"
```

このワークスペースのルートでは、どちらの行も通常は不要です。`uv sync --dev`（CI が実行するコマンドで、`uv sync` のデフォルト動作でもあります）は、`dev` 依存グループを通じて、すでに `mcp`、`vision` （頭部カメラ、`capture`）、`voice`（マイク側のスタック）を導入します。`--extra` フラグが必要になるのは、このワークスペースの外で `palmimo_sdk` を単独で使う場合です。その場合は、こうした設定は適用されません。

サーボバス（`dynamixel-sdk`）、フェイスディスプレイ、スピーカーの依存関係は、このワークスペースのデフォルトですでにインストールされます。そのため、ここではモーション、表情、発話のために追加の extra は不要です。ワークスペースの外で `palmimo-sdk` を単独でインストールする場合は、それぞれに `[hardware]`、`[face]`、`[speech]` を追加します。

## 実行

ロボットと同じマシン上のクライアントには、stdio 経由で提供します。

```bash
uv run python -m palmimo_sdk.mcp
```

最初に `PALMIMO_MCP_TOKEN` を未設定にしてください（または、コマンドの前に `env -u PALMIMO_MCP_TOKEN` を付けてください）。stdio はトークンを検証できないため、設定したままだと、認証なしで提供する代わりに、このコマンドは意図的に失敗します。理由は[認証](#認証)を参照してください。

または、ネットワーク経由で提供します（streamable HTTP。ロボットとは別のマシン上のクライアント用です。たとえば、Pi 上で動く Palmimo に対して PC から開発する場合です）。

```bash
uv run python -m palmimo_sdk.mcp --transport http --host 0.0.0.0 --port 8765
```

⚠️ トークンがなければ、このサーバーには**認証がありません**。ループバック以外の `--host`（`0.0.0.0` など）にバインドするのは、信頼できるネットワーク上だけにしてください。 `PALMIMO_MCP_TOKEN=<secret>` を設定すると（http トランスポートのみ）、すべてのリクエストに、一致する `Authorization: Bearer <secret>` ヘッダーを要求します。

```bash
PALMIMO_MCP_TOKEN=<secret> uv run python -m palmimo_sdk.mcp --transport http --host 0.0.0.0 --port 8765
```

## Claude Code への登録

```bash
# Local (client runs on the same machine as the robot, stdio transport). The
# client's cwd is not necessarily this repository's root, so point `uv run`
# at it explicitly with `--directory` (the trailing `palmimo-devkit` is the
# directory name `git clone` creates by default; adjust it if you renamed
# that directory):
claude mcp add palmimo -- uv run --directory <path-to>/palmimo-devkit python -m palmimo_sdk.mcp

# Remote (client is elsewhere; the robot runs the http transport command above)
claude mcp add --transport http palmimo http://<robot-host>:8765/mcp

# Remote, with a token configured: pass the same secret back as a Bearer header.
# The name and URL come FIRST here: `--header` is variadic, so leading with it
# swallows every following word -- the server name and the URL included, which
# is why the command then fails with `missing required argument 'name'`.
claude mcp add palmimo http://<robot-host>:8765/mcp --transport http --header "Authorization: Bearer <secret>"
```

## CLI フラグ

| フラグ | デフォルト | 意味 |
|---|---|---|
| `--transport {stdio,http}` | `stdio` | MCP のトランスポート。 |
| `--host` | `127.0.0.1` | `--transport http` のバインドアドレス。 |
| `--port` | `8765` | `--transport http` のバインドポート。 |
| `--token` | `$PALMIMO_MCP_TOKEN` | すべてのリクエストに `Authorization: Bearer <token>` を要求します（`--transport http` のみ）。フラグより環境変数を推奨します。コマンドラインの値は、`ps` を通じて同じマシンの他のユーザーに見えます。 |
| `--servo-port` | 自動検出 | サーボバスのシリアルポートを明示的に指定します。 |
| `--no-servo` / `--no-display` / `--no-speaker` / `--no-camera` | 無効 | 対応する周辺機器の接続（したがって検出のためのアクセスも）を省略します。開発用 PC で実行する際、検出処理がウェブカメラやデフォルトの音声デバイスを占有するのを避けるのに便利です。 |
| `--speaker-device` | `ReSpeaker` | `say` の出力先となる ALSA 再生カードです。USB の抜き差しで変わり得るインデックスではなく、カード ID または長い名前の部分文字列で照合します。ALSA のデフォルトを使う場合は空文字列を渡します。 |
| `--include` | （すべてのツール） | 公開するツールを限定するための、カンマ区切りのツール名です。 |
| `--exclude` | （なし） | 非表示にするツール名をカンマ区切りで指定します。 |
| `--log-tool-calls {off,summary,full}` | `$PALMIMO_MCP_LOG_TOOL_CALLS`、未設定なら `summary` | 各ツール呼び出しを stderr にどこまで記録するかを指定します。[ツール呼び出しのログ](#ツール呼び出しのログ)を参照してください。 |

## 認証

2 つのトランスポートでは、トークンの扱いが異なります。

- **stdio**（ロボットと同じマシン上のクライアント）には、Bearer トークンの検証を差し込めるリクエストの境界がないため、クライアントを一切認証できません。起動するシェルで `PALMIMO_MCP_TOKEN` が設定されている場合、トークンで保護されているように見えるシェルから黙って認証なしで提供する代わりに、コマンドは使用方法のエラー（終了コード 2）で起動を拒否します。変数を未設定にするか、コマンドの前に `env -u PALMIMO_MCP_TOKEN` を付けてください。
- **streamable HTTP**（ロボットとは別のマシン上のクライアント）は、トークンを要求できます。`PALMIMO_MCP_TOKEN=<secret>` を設定すると、すべてのリクエストに一致する `Authorization: Bearer <secret>` ヘッダーが必要になります。トークンがない場合、ループバック以外のホストにバインドしたサーバーには認証が一切ありません。 `0.0.0.0` などにバインドするのは、信頼できるネットワーク上だけにしてください。

`--token <secret>` は環境変数と同じ働きをし、両方を指定すると優先されます。ただし、環境変数を推奨します。コマンドラインの値は、`ps` の出力などのプロセス一覧を通じて、同じマシン上の他のローカルユーザーに見えるためです。

## 周辺機器がない場合の縮退動作

サーバーの起動時に接続できなかった周辺機器も、ツール一覧からは隠しません。そのツールは一覧に残り、呼び出すと未接続であることを説明するメッセージを返します。ツールが消えたり、サーバーが起動を拒否したりはしません。サーバーは `initialize` で返す `instructions` にこの動作を記載するので、クライアントは最初から把握できます。

## ツール呼び出しのログ

すべてのツール呼び出しを **stderr** に記録します。呼び出しごとに `INFO` を 1 行出力し、ツール名、引数名、結果（`ok`、`error`、`interrupted`）、所要時間、結果のサイズを記録します。

```
2026-08-09 12:00:00,000 INFO palmimo_sdk.mcp.server tool call name=forward args={seconds=1.5} outcome=ok duration_ms=1512.4 result=<str, 22 chars> images=0
```

stdio トランスポートでは stdout が MCP プロトコル自体を運ぶため、stdout には何も書き込みません。`--log-tool-calls`（または `PALMIMO_MCP_LOG_TOOL_CALLS`）で、呼び出しのどこまでをログ行に含めるかを設定します。

| 値 | ログ行に含めるもの |
|---|---|
| `summary`（デフォルト） | 引数名と、数値の引数の値です。文字列の値は長さだけにするため、呼び出し側が渡したテキスト（`say` の発話テキストや、将来のツールが受け取るテキスト）がログにコピーされることはありません。 |
| `full` | すべての引数の値と、結果のテキストをそのまま記録します。特定の呼び出しのデバッグ用です。 |
| `off` | 呼び出しごとのログ行を出力しません。 |

```bash
PALMIMO_MCP_LOG_TOOL_CALLS=full uv run python -m palmimo_sdk.mcp
```

## 関連ドキュメント

- [`build_mcp_server()`](../reference/api-reference.md#palmimo_sdkmcp) — この CLI の背後にある API
- [OpenClaw 接続キット](../../../examples/agents/openclaw/README.md) — コンテナからこのサーバーを動かす
