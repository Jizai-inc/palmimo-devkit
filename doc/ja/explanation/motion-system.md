# Palmimo の動きの仕組み

歩行とモーションの計算は、`palmimo_sdk` の `MotionEngine` に集約しています。
歩容の方向とパラメーターの唯一の正本です。Python API と
`examples/` 以下のサンプルエージェントアプリを含むすべてのフロントエンドは、
これを通じて動作します。

| 経路 | 適した用途 | 呼び出し方法 | 実装ファイル |
|------|--------------|-----------|------------|
| **MotionEngine + Python API** | 自分のスクリプトから歩行を動かす / 振り付けを組み立てる | `from palmimo_sdk import Palmimo` | [engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py), [robot.py](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) |

> **注記**: SDK 上に構築したフロントエンドは、脚の歩容を同じ
> `MotionEngine` に委ねます。歩容パラメーターの調整や新しい歩容の実装は、
> フロントエンドではなくエンジンで行ってください。

## 運動学チェーン

URDF から取得した値です（[engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) の
`MotionEngine` クラス変数 `L1` / `L2` / `L3`）。

| 記号 | 値（mm） | 部位 |
|-----|--------|------|
| `L1` | 32.0 | コクサ（取り付け部から pitch1） |
| `L2` | 45.3 | フェムル（pitch1 から pitch2） |
| `L3` | 100.0 | ティビア（pitch2 から足先） |

## トライポッド歩容を使う理由

[engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) の
`MotionEngine.TRIPOD_A` / `TRIPOD_B` は、6 本の脚を
2 つのグループに分けます。

| グループ | 脚 |
|---------|------|
| トライポッド A | 脚 1（RL）/ 脚 3（FL）/ 脚 5（MR） |
| トライポッド B | 脚 2（ML）/ 脚 4（RR）/ 脚 6（FR） |

A と B は常に逆位相で動くため、どの瞬間も 3 本の脚が地面に接し、
胴体が動的にバランスを取る必要はありません。脚番号と
実際の位置の対応（例: RL = 左後方）は、
[motor_layout.drawio.svg](../../images/motor_layout.drawio.svg) を参照してください。

全体像は [engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) の
コメントとコードを読むと分かります。上記の定数は、
読む前に知っておくと役立つものです。

## 関連ドキュメント

- [利用可能なモーションと歩容パラメーター](../reference/motions.md) — モーション一覧と調整項目
- [モーションの制御](../guides/controlling-motions.md) — Python からロボットを動かす方法
- [システムアーキテクチャ](architecture.md) — スタック内でのエンジンの位置付け
