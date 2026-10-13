# モーションと歩容パラメーター

## 利用可能なモーション

[engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) の `Motion` 列挙型で定義されています。次の 18 種類を Python API から呼び出せます。

| 列挙値 | 文字列名 | 説明 |
|--------|--------|------|
| `IDLE` | `idle` / `stop` | ニュートラル姿勢に滑らかに戻ります |
| `FORWARD` | `forward` | 前進します（トライポッド歩容） |
| `BACKWARD` | `backward` | 後退します（トライポッド歩容） |
| `STRAFE_LEFT` | `strafe_left` | 左へ平行移動します |
| `STRAFE_RIGHT` | `strafe_right` | 右へ平行移動します |
| `ROTATE_LEFT` | `rotate_left` | その場で反時計回り（CCW）に旋回します |
| `ROTATE_RIGHT` | `rotate_right` | その場で時計回り（CW）に旋回します |
| `CREEP` | `creep` | 一度に 1 本の脚を動かす低速のクリープ歩容です（最も安定しています） |
| `DANCE` | `dance` | 胴体を揺らしてダンスします |
| `BODY_TILT` | `body_tilt` / `tilt` | 左右に傾きます（「興味を示す」ポーズ） |
| `PUSHUP` | `pushup` / `push_up` | 胴体を上下させて腕立て伏せをします |
| `WAVE` | `wave` / `wave_right` / `wave_left` | デフォルトでは右前脚で挨拶します（`wave_right`）。`wave_left` は左右を反転し、左前脚で行います |
| `WAVE_BOTH` | `wave_both` | 両前脚で同時に挨拶します（まず中央の脚を前に出し、胴体を後ろに傾けて鼻先を上げた「おねだり」のポーズを取り、続いて両脚を振ります） |
| `CLAP` | `clap` | おねだりのポーズを保ち、両前脚（手）をヨー軸で開閉して拍手します（足先が接触しないよう下限を設けています） |
| `BOW` | `bow` | 前脚を上げて胸と頭を下げ、お辞儀します（姿勢を整える → 保持する → ゆっくり起き上がる、1 回のみ） |
| `STRETCH` | `stretch` | 足先を接地させたまま全脚の大腿部を折り曲げ、胴体を持ち上げて伸びをします（姿勢を整える → 持ち上げる → 保持する → 下ろす、1 回のみ） |
| `NOD` | `nod` | 「はい」とうなずきます（首のみ、1 回のみ） |
| `HEAD_SHAKE` | `head_shake` / `shake_head` | 「いいえ」と首を振ります（首のみ、1 回のみ） |

`set_motion(name)` でモーションを文字列指定するためのキーの一覧は、 [robot.py](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) の `Palmimo._MOTION_MAP` で定義されています。

各モーションを開始するメソッドについては、 API リファレンスの[コアメソッド](api-reference.md#コアメソッド)を参照してください。

## 歩容パラメーター

[engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) の `MotionEngine.__init__` で定義されています。

| パラメーター | デフォルト | 単位 | 説明 | 設定方法 |
|----------|------|-----|------|------|
| `gait_speed` | `0.012` | 0-1 | ステップごとの位相の増分（1 = 1 周期） | `MotionEngine` または `Palmimo` のコンストラクター |
| `step_length` | `30.0` | mm | 前進方向の足先の歩幅 | `MotionEngine` または `Palmimo` のコンストラクター |
| `step_height` | `30.0` | mm | 遊脚中の足先の持ち上げ高さ（歩行と旋回に適用） | `MotionEngine` または `Palmimo` のコンストラクター |
| `yaw_amplitude` | `250` | tick | 歩行中のヨー関節の最大振幅（中心 2048 からのオフセット） | `MotionEngine` のコンストラクターのみ。`Palmimo` はこの値を転送しません |

`Palmimo` インスタンスから、転送されない調整値にアクセスするには、そのインスタンスが所有するエンジンを使います。`robot.engine.yaw_amplitude = 200`。

ジェスチャーごとの調整（手を振る、うなずく、首を振る、ダンス）は、 [API リファレンス](api-reference.md)に記載しています。

## 実装ファイル

| パス | 役割 |
|-----|------|
| [packages/palmimo_sdk/palmimo_sdk/engine.py](../../../packages/palmimo_sdk/palmimo_sdk/engine.py) | 歩行エンジン本体（IK + 歩容生成、純粋な計算） |
| [packages/palmimo_sdk/palmimo_sdk/robot.py](../../../packages/palmimo_sdk/palmimo_sdk/robot.py) | 高水準の Python API（`Palmimo` クラス、振り付けの再生） |
