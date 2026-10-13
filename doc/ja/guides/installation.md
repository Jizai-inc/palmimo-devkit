# インストール

ここにあるコマンドはすべて、README があるリポジトリのルートから
実行してください。前提条件は **Python 3.12+** と [**uv**](https://docs.astral.sh/uv/) です。

## 想定環境

- 実行ホスト: **Raspberry Pi**（デフォルトの想定）— 制御ループからサーボへの書き込みまで、すべてここで実行します。
- 開発マシン: PC（macOS / Linux）— 編集、起動操作、ログの閲覧に使います。制御には関与しません。
- インターフェース基板: サーボバスの USB–サーボブリッジ

実行モデルの全体像は [architecture.md](../explanation/architecture.md)（正式な設計ドキュメント）を参照してください。
Raspberry Pi の構築手順は [raspberry-pi-setup.md](raspberry-pi-setup.md) を参照してください。

## 依存関係の解決

```bash
uv sync
```

これにより、すべてのオプションの extra を含めて SDK がインストールされます。`uv sync` は
`dev` 依存グループを含み、そのグループがすべての extra を要求するためです。共有マイク
ストリーム（`MicStream`）、GTCRN によるノイズ除去（`palmimo_sdk.audio`）、piper-plus
TTS、頭部カメラ、MCP サーバーが含まれます。`--no-dev` を指定すると、コア
（モーター制御）だけをインストールします。`palmimo_sdk` 自身の extras は、ワークスペースの
ルートでも同じ名前で指定できます（例: `uv sync --no-dev --extra voice`）。これは、
`palmimo_sdk` を単独のライブラリとして使う場合と同じです。

`examples/` 以下のサンプル（ウェイクワードエージェント、コンパニオンエージェント、
`palmimo_teleop`）は、それぞれ独立した uv プロジェクトであり、このワークスペースの
メンバーではありません。ここで `uv sync` を実行しても、サンプルの依存関係はインストールされません。
インストールと実行の方法は [examples/README.md](../../../examples/README.md) を参照してください。

### このクローンの外で `palmimo_sdk` をライブラリとして使う

上の手順は、このリポジトリ自身のワークスペースをセットアップするものです。
代わりに別のプロジェクトで `palmimo_sdk` を使うには、公開されているパッケージを
直接追加します。

```bash
uv add palmimo-sdk
```

extras（`hardware`、`voice`、`agent`、...）と最小限のサンプルは、
[`packages/palmimo_sdk/README.md`](../../../packages/palmimo_sdk/README.md) を参照してください。

## 音声出力（piper-plus TTS）

ワークスペースをインストールすると、`palmimo_sdk.audio` が音声出力に使う
音声合成エンジン piper-plus が導入されます。音声モデル自体は
同梱されていませんが、手動で取得する必要はありません。
`Speaker.open()`（スピーカーが接続されている場合に `robot.connect()` が
呼び出します）が、必要な音声を初めて使うときに取得します。
低速な回線では初回に音声 1 つあたり 1 分程度かかり、ネットワーク接続が必要です。
サイズはそれぞれ約 38 MB です。2 回目以降はディスク上の音声を使います。

### 音声出力の初回セットアップ

音声モデルと、英語の音素変換に必要な NLTK データは初回使用時に自動ダウンロード
されるため、ネットワークに接続されたマシンでは手動の操作は不要です。これらは
`$XDG_CACHE_HOME/palmimo/models`（`XDG_CACHE_HOME` が未設定なら
`~/.cache/palmimo/models`）にまとめてキャッシュされます。Palmimo Portal サービスが使う、
制限されたホームディレクトリでも動作します。

オフラインのマシンでは、ネットワークに接続されたマシンで NLTK ダウンローダーを実行し、
生成された `nltk_data` ディレクトリを同じキャッシュ先にコピーしてください。

```bash
uv run python -m nltk.downloader \
  --download-dir ~/.cache/palmimo/models/nltk_data \
  averaged_perceptron_tagger averaged_perceptron_tagger_eng cmudict
```

オフラインのマシンでは、ネットワークに接続されたマシンで音声を取得してディレクトリ
全体をコピーするか、次のように明示的に事前取得してください。

```bash
uv run piper --download-model ja_JP-tsukuyomi-chan-medium \
  --download-dir ~/.cache/palmimo/models/piper/ja_JP-tsukuyomi-chan-medium
```

### 音声のキャッシュ先

音声はリポジトリの外に、音声ごとに 1 つのディレクトリでキャッシュされます。

```
~/.cache/palmimo/models/piper/ja_JP-tsukuyomi-chan-medium/   # $XDG_CACHE_HOME is honored
~/.cache/palmimo/models/piper/ja_JP-css10-6lang-medium/      # (English uses the multilingual model)
~/.cache/palmimo/models/nltk_data/                            # English phonemization data
```

音声ごとのディレクトリ分けは、正しく動かすために必要です。piper-plus のカタログにある音声は、
設定ファイルの名前がすべて `config.json` なので、同じディレクトリに置くと
互いの設定を上書きしてしまいます。検索するのは音声ごとのディレクトリだけです。piper 自身の
フラットな配置は、組み合わせを取り違えるため検索しません。`PiperEngine(data_dir=...)` で
ルートを別の場所に移しても、その中の構成は同じです。

### 言語ごとのダウンロード動作

`Speaker` は、開くときに指定された音声（`SpeakerConfig(lang=...)`）だけを、
`open()` 時にのみダウンロードします。もう一方の言語がない場合は警告しますが、
後から取得することはありません。発話中にダウンロードすると音声が止まり、
`disconnect()` も待たされるためです。両方を話すには（`say(lang=...)` または
`say_bilingual`）、上のオフライン用コマンドでもう一方の音声を事前取得してください。

### 古いチェックアウトからのアップグレード

リポジトリのルート、または `data_dir` 直下に置かれたモデルは、
見つからなくなっています。各音声の `.onnx` と設定ファイルを、その音声名の
ディレクトリに移すか、初回実行時に再ダウンロードさせてください。

### ディスク使用量

音声ごとに 38 MB ではなく、約 115 MB を見込んでください。音声を初めて
*読み込む*とき、piper-plus は ORT 最適化済みのモデルのコピー
（`*.cpu.opt.onnx`、約 75 MB）を隣に書き込み、以降の実行で再利用します。
`PIPER_DISABLE_CACHE=1` で無効にできますが、読み込みが遅くなります。

### `ja_JP-tsukuyomi-chan-medium` のライセンスに関する注意

この音声には、piper-plus /
pyopenjtalk-plus とは独立したライセンスがあります。商用・非商用ともに無料で使えますが、
この音声の出力を動画、配信、その他の形で公開する場合は、
クレジット表記が必要です。公式の利用条件と必要な表記は
https://tyc.rei-yumesaki.net/material/corpus/ を参照してください（詳細:
[`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)）。

## トラブルシューティング

| エラー | 対処 |
|--------|------|
| `ModuleNotFoundError: dynamixel_sdk` | `uv sync` |
| `ModuleNotFoundError: sounddevice` / `sherpa_onnx` | `uv sync`（サンプルの実行で発生したエラーなら、そのサンプル自身のディレクトリ内で実行）。`palmimo_sdk` を単独で使う場合は `uv sync --extra voice` |
| `ModuleNotFoundError: cv2` / `mediapipe` | `uv sync`（サンプルの実行で発生したエラーなら、そのサンプル自身のディレクトリ内で実行）。`palmimo_sdk` を単独で使う場合は `uv sync --extra vision` |
| `RuntimeError: piper-plus is not installed` | `uv sync`。`palmimo-sdk[speech]` 経由で piper-plus をインストールします |
| `RuntimeError: failed to download piper voice model ...` | 初回の音声ダウンロード時にネットワークに接続できませんでした。接続するか、上で説明した方法で音声ディレクトリをコピーしてください。 |
| `RuntimeError: no audio player available` | `Speaker` は合成音声を `aplay`/`ffplay`（Linux）または `afplay`（macOS）で再生します。いずれかをインストールしてください（例: `sudo apt install alsa-utils`） |

## 関連ドキュメント

- [Raspberry Pi のセットアップ](raspberry-pi-setup.md) — 制御ホストの準備
- [モーションの制御](controlling-motions.md) — インストール後に最初に実行するもの
- [システムアーキテクチャ](../explanation/architecture.md) — 何をどこで動かすか、その理由
