# Raspberry Pi のセットアップ（ヘッドレス）

Palmimo を動かす Raspberry Pi を、**Mac にケーブルで接続せずに**
セットアップする方法です。Raspberry Pi Imager は SSH、WiFi、公開鍵を**書き込み時に**
カードへ組み込むため、カードを挿して電源を入れると、
Pi に SSH で接続できます。

> このガイドは、開発用 PC から切り離し、**Pi 単体で** Palmimo を動かすための
> 最初の具体的な手順です。この実行モデルは
> [explanation/architecture.md](../explanation/architecture.md#execution-model) で説明しています。
> アーキテクチャは変わりません。`palmimo_sdk` を実行するホストが PC から
> Pi に移るだけです。制御ループ（歩容の生成 → サーボへの書き込み、60 Hz）は
> すべて Pi 上で動き、サーボバスの USB アダプターは Pi に直接接続する前提です。
> 設計目標と安全要件については、引き続き
> architecture.md を正式な情報源とします。

## 0. 前提条件

- ワークステーションに [Raspberry Pi Imager](https://www.raspberrypi.com/software/) が
  インストールされていること
- microSD カード（または USB SSD）とカードリーダー
- 接続先の WiFi ネットワークの **SSID、パスワード、国コード**（例: `JP`）
- ワークステーション上の SSH 公開鍵（`ssh-keygen -t ed25519` で作成 →
  `~/.ssh/id_ed25519.pub`）

> DevKit の制御ホストには **Raspberry Pi 5** が付属し、イメージは
> 書き込み済みです。第 1〜2 節ではカードに一から書き込みます。ここで自分の Wi-Fi の
> 接続情報を登録します。第 3 節ではクイックスタートが前提とするランタイムを
> インストールします。同梱カードへの再書き込みや、自分で用意した Pi の準備に使ってください。
> 必須条件は **64 ビット（aarch64）**の OS です。
> `opencv-python` や `piper-plus` などの依存パッケージは aarch64 の
> wheel を提供しますが、armv7l の wheel は提供しません。そのため 32 ビットの Pi OS では依存関係を解決できません。
> [I/O ランタイム環境の要件](../explanation/architecture.md#io-runtime-environment-requirements)を参照してください。

## 1. ヘッドレスでカードに書き込む

Imager の **OS customisation** で、有線接続なしで SSH に接続するために必要な設定を
すべて組み込みます。これがこのガイドの中心です。

1. Imager を起動 → **Device** で対象のボードを選びます
2. **OS** で `Raspberry Pi OS (64-bit)` を選びます。Lite で十分です。GUI は不要です
3. **Storage** で SD カードを選びます
4. **Next** → OS のカスタマイズを適用するか聞かれたら、**Edit Settings** を選びます

> **デフォルトに頼らず、音声デバイスを名前で指定してください。** Lite イメージには、出力先を
> 自動で選ぶデスクトップ用の音声スタックが含まれていません。
> ALSA は登録順にカード番号を付けます。起動後に接続した USB マイク
> アレイは内蔵 HDMI 出力の後になるため、
> デフォルトの出力先にはスピーカーが接続されていない場合があります。代わりに
> `SpeakerConfig(device_name_hint="ReSpeaker")` と
> `MicrophoneConfig(device_name_hint="ReSpeaker")` を渡してください。SDK はヒントを
> カード ID と照合します。カード ID はインデックスが変わっても変わりません。
> `/etc/asound.conf` や `~/.asoundrc` にデフォルトを設定する方法もありますが、
> そこで固定したインデックスは、次の抜き差しで無効になる可能性があります。

### カスタマイズする値

| 項目 | 値 | 備考 |
|---|---|---|
| ホスト名 | `palmimo`（→ `palmimo.local`） | mDNS でホストを名前解決できるよう、この名前を推奨します |
| ユーザー名 | 任意（以下では `<pi-user>`） | 自由です。ここではユーザー名に依存するものはなく、自動起動用に追加する systemd ユーザーサービスも、固定名の代わりに `%h` でホームディレクトリを解決できます |
| パスワード | 任意の強力な値 | ログインには鍵認証を使います。これは `sudo` 用の代替手段です |
| WiFi SSID / パスワード | 控えておいた値 | **この設定により有線 LAN が不要になります。** 2.4/5 GHz の対応はボードによります |
| WiFi 国コード | `JP` など | 未設定だと WiFi が無効のままになる場合があります |
| ロケール / タイムゾーン | `Asia/Tokyo` など | ログのタイムスタンプを揃えます |

**Services** タブで、次を設定します。

- **Enable SSH** → **Allow public-key authentication only** を選びます
- `~/.ssh/id_ed25519.pub` の内容を貼り付けます（複数人が
  アクセスする場合は、1 人につき 1 つの鍵を追加します）

> セキュリティポリシーは**鍵認証のみ**です。Imager に渡すのは公開鍵
> だけにしてください。**秘密鍵やパスワードは絶対にリポジトリにコミットしないでください**。

5. **Save** → **Write**。完了したらカードを Pi に挿します。

## 2. 初回起動と SSH

カードを挿し、Pi の電源を入れます（**Mac には接続しないでください**）。初回
起動では WiFi への接続と初期化に 1〜2 分かかります。

同じ LAN 内のマシンから接続します。

```bash
ssh <pi-user>@palmimo.local
```

> `palmimo.local` を名前解決できない場合は、ルーターの DHCP
> テーブルで Pi のアドレスを調べ、`ssh <pi-user>@<IP>` で接続します。`~/.ssh/config` に登録すると、
> 以降の操作が簡単になります。
>
> ```
> Host palmimo
>     HostName palmimo.local
>     User <pi-user>
>     ServerAliveInterval 60
> ```
>
> 以降は `ssh palmimo` で接続でき、VS Code Remote-SSH も同じホスト設定を使えます。

## 3. ランタイムを準備する（Pi 上）

```bash
# Access to the USB serial devices for the servo bus and the display. Requires re-login
sudo usermod -aG dialout "$USER"

# Lite ships without git, and the Quickstart clones this repository with it
sudo apt-get update
sudo apt-get install -y git

# Install uv (never pip — see AGENTS.md)
curl -LsSf https://astral.sh/uv/install.sh | sh
# Reopen the shell, or source ~/.bashrc, to pick up PATH
```

`dialout` グループへの所属を反映するため、一度ログアウトしてからログインし直します。

> この `PATH` の設定が届くのは対話シェルだけです。非対話型の SSH
> 実行（`ssh <pi-user>@palmimo.local '<command>'`）では
> `~/.bashrc` を読まないため、この方法で `uv` を動かそうとすると
> `uv: command not found` と終了ステータス 127 で失敗します。リモートコマンドは
> `ssh <pi-user>@palmimo.local 'bash -lc "<command>"'` のようにログインシェルで包んでください。
> `uv` を `PATH` に追加するプロファイルを先に読み込めます。対話型の
> `ssh palmimo` セッションは影響を受けません。

> **通常、ポートを指定する必要はありません。** サーボバスの USB アダプターは Linux では
> `/dev/ttyACM*` として現れます。ただし、デバイスを抜き差ししたり、
> 複数のシリアルデバイスが共存したりすると番号が変わることがあります（フェイスディスプレイも `ttyACM` です）。
> `find_servo_port()` は文字列と VID の照合でデバイスを識別するため、番号が変わっても
> 影響を受けません。特定の理由で固定する必要がある場合にだけ、
> `--port` を明示的に渡してください。

### マイクとカメラのシステム依存関係

実際のマイクとカメラのバックエンドには、Python の依存関係の同期では
インストールされない OS ライブラリが必要です。必要なものはアプリによって異なります。

| アプリと依存関係 | 録音 / 撮影に使うもの | OS の依存関係 |
|---|---|---|
| SDK の `HeadCamera` | `opencv-python` | `v4l-utils` |
| SDK の TTS（`palmimo_sdk.io.speaker`） | `piper` バイナリ | 音声モデル以外は不要 |
| SDK の `MicStream` / ウェイクワードエージェントのサンプル | `sounddevice` | `libportaudio2` |

カメラを使うすべてのアプリは、同じカメラ処理を共有します。`opencv-python` には
V4L2 ドライバー、`/dev/video<id>` の読み取り権限、そして Lite イメージでは
wheel がリンクする OpenGL ランタイムが必要です。`libgl1` がないとインポート自体が
`ImportError: libGL.so.1: cannot open shared object file` で失敗します。その結果、
MCP サーバーの `capture` ツールが無効になり、コンパニオンエージェントは
起動できなくなります。

```bash
sudo apt-get update
sudo apt-get install -y v4l-utils libgl1
v4l2-ctl --list-devices                 # list connected cameras
```

SDK の `sounddevice` ベースのマイク収音（`MicStream`、
ウェイクワードエージェントのサンプル）を使うアプリには、さらに **PortAudio C ライブラリ**が必要です。
これがないと、起動時に `OSError: PortAudio library not found` で失敗します。

```bash
sudo apt-get install -y libportaudio2
```

TTS 用の音声モデルのダウンロードは、
[インストールガイド](installation.md#first-time-setup-for-voice-output)で説明しています。

## 4. Pi 上で Palmimo を動かす

コードは Pi に置き、Pi 上で実行します。制御ループがネットワークを
またぐことはありません。ランタイムを用意した後の手順は、
[クイックスタート](../../../README.md#-quickstart)で説明しています。リポジトリをクローンし、
ワークスペースを同期し、サーボバスを確認します。

開発中は、ワークステーションから `ssh palmimo` で接続し、これらのコマンドを
Pi 上で実行します。自律動作時には SSH は一切不要です。目標は、電源投入時に安全な初期姿勢から
起動し、PC を切り離して動作させることです（
[実行モデル](../explanation/architecture.md#execution-model)を参照）。各アプリの自動起動は、
そのアプリ用の systemd ユーザーサービスで設定します。このリポジトリには含まれていません。

## 付録

### 古いホスト名からの移行

既存の Pi では、OS やカードを書き直す必要はありません。現在の SSH 接続から
ホスト名だけを変更します。再起動後は、既存のユーザー名で
`palmimo.local` に接続してください。

```bash
printf 'preserve_hostname: true\nmanage_etc_hosts: false\n' | sudo tee /etc/cloud/cloud.cfg.d/99-palmimo-hostname.cfg >/dev/null
sudo hostnamectl set-hostname palmimo
sudo sed -i 's/^127\.0\.1\.1.*/127.0.1.1 palmimo/' /etc/hosts
sudo reboot
```

Raspberry Pi OS の構成によっては、起動時に cloud-init が古いホスト名を再適用します。
上のコマンドは、ホスト名と `/etc/hosts` の管理を OS 自体に委ねるため、
再起動しても古い名前に戻りません。

Linux のユーザー名は変更しないでください。変更しても利点はなく、ホームディレクトリ、SSH、
ファイルの所有権に影響します。カードにイメージを書き込んだときのユーザー名で
接続してください: `ssh <pi-user>@palmimo.local`。

### ユーザー名を固定しない理由

このリポジトリには、アカウント名を指定するものはありません。自動起動用に追加する systemd ユーザーユニットは、
`%h` でホームディレクトリを解決できます。そのため、OS のアカウント名を
製品名に結び付けずに動作を維持できます。

## 関連ドキュメント

- [explanation/architecture.md](../explanation/architecture.md) — Pi の実行モデル、安全
  設計、I/O ランタイムの要件（設計の正式な情報源）
- [installation.md](installation.md) — 依存関係、音声モデル、トラブルシューティング
