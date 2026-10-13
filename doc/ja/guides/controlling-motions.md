# Python からモーションを制御する

自分のスクリプトに歩行を組み込むには、
[robot.py](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) の `Palmimo` クラスを使います。公開するメソッドはすべて
[コアメソッド](../reference/api-reference.md#core-methods)に記載しています。
このページでは、それらの呼び出し方を示します。

## 実行前に

| 手順 | 参照先 |
|-----|-----|
| すべてのモーターが応答することを確認する | [`diagnose_servos.py scan`](../../../scripts/README.md) |
| ニュートラル姿勢に戻す | `Palmimo.stop()` が滑らかにニュートラルへ戻します |
| トルクを無効にする（手で動かす） | アプリを終了する / 電源を切る（[安全上の注意](../../../README.md#-safety)を参照） |

新しいモーションを初めて実行するときは、本体を持ち上げて脚を浮かせてください。

## 最小限のサンプル（ドライラン）

```python
from palmimo_sdk import Palmimo

robot = Palmimo(gait_speed=0.012, step_length=30.0, step_height=30.0)
robot.forward()

for _ in range(120):              # ~2 seconds @ 60 Hz
    positions = robot.step()      # dict[str, int]: motor name -> tick (0-4095)
    print(positions)

robot.stop()
for _ in range(60):               # return to neutral
    robot.step()
```

> ⚠️ tick の範囲は 0-4095 ですが、モーションは安全範囲 **200-3900** 内に収めてください。
> 両端の値は機械的な限界です。
> [安全設計](../explanation/architecture.md#safety-design)を参照してください。

## 実際のハードウェアにも送る

`step()` の戻り値は、脚のモーター 18 個と
`neck_yaw`、`neck_pitch1`、`neck_pitch2` の計 21 個のモーターの目標 tick を格納する辞書です。
2 つの首のピッチ関節が視線の目標をどう分担するかは、
[コアメソッド](../reference/api-reference.md#core-methods)の `robot.look()` を参照してください。この戻り値は
`DynamixelDriver.write_positions()` が受け取るものなので、そこへ渡すと実機が動きます。具体的なバス初期化手順は、
[DynamixelDriver](../../../packages/palmimo_sdk/palmimo_sdk/io/dynamixel.py) と API リファレンスの
[周辺機器と接続](../reference/api-reference.md#peripherals--connection)
の節を参照してください。

## 振り付け

[robot.py](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) の `Palmimo.play()` は、
`(motion name, seconds)` のステップを順に再生します。特別なモーション
`look_around` は、首を正弦波状に往復させます。`(name, seconds)` の
タプル形式では `motion` がデフォルトの `Motion.IDLE` のままなので、その区間は脚が
動かないことに注意してください。歩きながら首を往復させるには、
タプルの代わりに `RoutineStep(motion=..., neck_sweep=True)` を渡します。

```python
routine = [
    ("forward",     2.0),
    ("rotate_left", 1.0),
    ("dance",       3.0),
    ("look_around", 2.0),
    ("idle",        0.5),
]

robot = Palmimo()
for positions in robot.play(routine, fps=60):
    # send positions to real hardware / a simulator
    ...
```

リアルタイム再生（内部で `time.sleep` を使って実行間隔を調整）には、
[`play_realtime`](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) を使います。

## トラブルシューティング

- **シリアルポートが見つからない**: `ls /dev/tty.usbmodem*`
  （macOS）または `ls /dev/ttyACM*`（Linux）で実際のデバイス名を調べ、
  `DynamixelDriver` を作成するときにポートを明示的に指定してください
- **モーターが動かない / ガタつく**: 電源の容量とケーブルの接続を確認し、
  `diagnose_servos.py scan` で各 ID が応答することを確かめてください
- **動きが不安定**: 必ず本体を持ち上げて脚を浮かせ、実機で実行して
  動作を確認してから、床や机に置いてください

## 関連ドキュメント

- [モーションと歩容パラメーター](../reference/motions.md) — 呼び出せるものと調整できる項目
- [Palmimo の動きの仕組み](../explanation/motion-system.md) — 歩容がこの設計になっている理由
- [モーション開発ガイド](motion-development-guide.md) — 新しいモーションの追加
