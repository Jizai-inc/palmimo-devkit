# リリース

SDK とサンプルカタログを、それぞれ独立してリリースする方法です。

SDK リリースは、検証済みの SDK リビジョンを示し、`palmimo-sdk` を
PyPI に公開します。サンプルリリースは、Palmimo Portal が使う
公式アプリカタログを公開します。どちらもデバイスに何かを配信するものではありません。
このリポジトリをクローンしている場合は、リリースアセットをダウンロードするのではなく、
タグをフェッチしてチェックアウトするか、`main` をプルして更新します。

Palmimo Portal は、独自のリポジトリで保守される別の製品です。
このリポジトリのリリースとは独立して、
そちらのリポジトリから自己更新します。

## 1. SDK リリース

- SDK のタグは `vX.Y.Z` 形式で、任意で `-rcN` というプレリリース接尾辞を付けます
  （`vX.Y.Z-rc1`）。リリースワークフローは、それ以外の形式を拒否します。
- **公開済みのタグは決して削除したり移動したりしないでください**。古いタグをその場で
  修正する代わりに、新しいタグを公開します。
- `-rcN` 接尾辞付きの SDK タグ（例: `v1.2.0-rc1`）は、自動的に GitHub の
  プレリリースとして作成されます（後述の [CI が行うこと](#ci-が行うこと)を参照）。
  `GET repos/{repo}/releases/latest` はドラフトとプレリリースをどちらも無視するため、
  プレリリースのビルドが誤って「最新リリース」になることはありません。

### タグを付ける前に

1. `pyproject.toml` と `packages/palmimo_sdk/pyproject.toml` の `version` を上げます。
2. 両方のロックファイルを再生成し、新しいバージョンを記録します。CI の `lock`
   ジョブと `integration-lerobot` ジョブがこれらを検査し、pyproject のバージョンだけを
   上げた場合は失敗します。

   ```bash
   uv lock
   cd integrations/lerobot && uv lock
   ```

3. 4 つのファイル（2 つの `pyproject.toml` と 2 つの `uv.lock`）を
   1 つのプルリクエストに含めます。マージし、`main` の CI が成功していることを確かめます。
4. タグを付ける前に、空のカードから [raspberry-pi-setup.md](raspberry-pi-setup.md) の
   手順を最初から最後まで実施し、ボード、イメージ、結果を
   リリースノートに記録します。
5. マージ済みのコミットにタグを付けます。

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

実行できる人: GitHub 組織の全メンバーがこのリポジトリへの書き込み権限を持ち、
その権限だけでタグのプッシュ、リリースワークフローの実行、
作成されたドラフトの公開を行えます。意図的に、
専用のリリース担当ロールは設けていません。ワークフローは `main` 上にない正式版タグを
拒否し、人による確認は公開前のドラフトの精読で行います。

### CI が行うこと

`v*` タグをプッシュすると、`.github/workflows/release.yml` が起動します。

1. タグの形式を検証し、正式版タグでは、そのタグが付いたコミットが
   `main` の祖先であることも検証します。無関係なコミットにプッシュされたタグからの
   リリースビルドは拒否します。`-rcN` タグはプルリクエストのブランチを指せるため、
   プルリクエストのマージ前に候補版をデバイスで検証できます。
2. GitHub が自動生成したノート付きで、リリースを**ドラフト**として作成します
   （内容の分類は `.github/release.yml` によります。後述の[ラベル](#3-ノートの分類に使うラベル)を
   参照）。`-rcN` タグは `--prerelease` 付きで作成されるため、
   `releases/latest` に現れることはありません。
すでにリリースが存在するタグでワークフローを再実行しても、
ドラフトか公開済みかにかかわらず何も行いません。リリースがすでに
**公開済み**のタグで再実行しても、変更は拒否されます。
代わりに新しいタグを作成してください。

### 公開

1. GitHub でドラフトリリースを開きます。
2. 自動生成されたノートを後述の[テンプレート](#リリースノートのテンプレート)と照らし合わせ、
   手書きの冒頭ブロックを貼り付けます。
3. 正式リリース（プレリリースではない場合）では、**「Set as the latest
   release」**にチェックを入れます。
4. 公開します。

公開すると `.github/workflows/publish.yml` が起動します。`palmimo-sdk` をビルドし、
TestPyPI にアップロードしてから、そのビルドをインストールしてスモークテストを行い、
成功した場合に限り、同じビルドを PyPI にアップロードします。プレリリースは TestPyPI の
スモークテストまでで終了し、PyPI には公開しません。`workflow_dispatch` では、リリースせずに、
リハーサルとして同じ TestPyPI とスモークテストの処理を必要に応じて実行します。
この場合は `X.Y.Z` ではなく `X.Y.Z.dev<run id>` をアップロードするため、リハーサルが
後のリリースで必要になる TestPyPI のバージョンを使用済みにすることはありません。

`publish.yml` が失敗したら、Actions ページから失敗したジョブを再実行します。PyPI は
すでに存在するバージョンを受け付けないため、`pypi` へのアップロード自体が
完了した後は再実行できません。代わりに問題を修正し、新しい
パッチバージョンを作成してください。

## 2. サンプルカタログのリリース

カタログのタグは `examples-vX.Y.Z` 形式で、任意で `-rcN` というプレリリース
接尾辞を付けます（`examples-vX.Y.Z-rc1`）。SDK のバージョンが変わらなくても、
カタログの変更にはサンプル用のタグを使います。公開済みのタグは決して削除したり移動したりせず、
代わりに新しいタグを公開してください。

1. サンプルとマニフェストの変更を `main` にマージし、CI が成功していることを確かめます。
2. そのマージ済みコミットにタグを付け、タグをプッシュします。

   ```bash
   git tag -a examples-vX.Y.Z -m "examples-vX.Y.Z"
   git push origin examples-vX.Y.Z
   ```

3. GitHub でドラフトリリースを開き、自動生成されたノートを読んでから公開します。
   「Set as the latest release」にはチェックを入れません。このリポジトリの最新
   リリースは SDK のリリースであり、Portal は `releases/latest` ではなく `examples-v`
   タグでカタログを見つけます。

どちらのタグをプッシュしても `.github/workflows/release.yml` が実行されます。
SDK タグと同じタグ形式と `main` の検査を行い、自動生成されたノート付きの
ドラフトリリースを作成します。`-rcN` タグには GitHub のプレリリースの印を付けます。
`examples-v*` タグの場合に限り、すべてのサンプルマニフェストから
`palmimo-catalog-<tag>.json` とその `.sha256` も生成し
（[アプリマニフェスト](../reference/app-manifest.md)を参照）、両方を
ドラフトに添付します。サンプルリリースの公開では `publish.yml` を呼び出さないため、
TestPyPI や PyPI に公開することはありません。

Palmimo Portal は `examples-v` リリースからのみカタログを読み込みます。リリースの
選択ロジックは Portal のリポジトリで保守します。

## 3. ノートの分類に使うラベル

**マージ前に**プルリクエストに次のいずれかのラベルを付けると、
`.github/release.yml` が適切な見出しに振り分けます。

| ラベル | 見出し |
|---|---|
| `breaking-change` | Breaking changes |
| `sdk`, `motion` | SDK & motions |
| `example`, `integration` | Examples & integrations |
| `bug` | Fixes |
| `documentation` | Documentation |
| `skip-changelog` | 完全に除外 |
| `dependencies` | 完全に除外 |
| （上記のいずれでもない） | Other changes |

## 4. リリースの検証

```bash
gh release view vX.Y.Z
gh release view examples-vX.Y.Z
```

## リリースノートのテンプレート

GitHub には自由記述のリリーステンプレートファイルはありません。`.github/release.yml` は
自動生成される「What's Changed」節の構成を決めるだけです。公開時に、この手書きの
ブロックをその上に貼り付けます。

```markdown
## Highlights

- 2-4 bullets on what this release is for

## Upgrade notes

- Anything a user must do or expect when updating a clone to this tag

## Known issues

- Anything shipped with a known gap, and its workaround if any

<!-- GitHub's generated "What's Changed" section follows below -->
```
