# モーション開発ガイド

Palmimo に新しいモーションを追加する手順を説明します。

## 前提条件

- [explanation/architecture.md](../explanation/architecture.md) を読み、責務の分離を理解します
- サーボの配置と脚の番号は [motor_layout.drawio.svg](../../images/motor_layout.drawio.svg) を参照してください
- サーボの可動範囲の制限は README の[安全上の注意](../../../README.ja.md#-安全上の注意)を参照してください
- [installation.md](installation.md) のセットアップを完了します
- ハードウェア診断を実行する前に、[リソース予約エラー](../reference/api-reference.md#リソースの予約)に対処してください。報告された占有元を停止してから再試行します。

## ステップ 1: モーションを定義する

`engine.py` の Motion 列挙型に追加します。

```python
class Motion(Enum):
    # ... existing motions ...
    MY_NEW_MOTION = auto()
```

既存のモーション: IDLE, FORWARD, BACKWARD, STRAFE_LEFT, STRAFE_RIGHT, ROTATE_LEFT, ROTATE_RIGHT, DANCE, BODY_TILT, PUSHUP, WAVE, WAVE_BOTH, CLAP, CREEP, BOW, STRETCH, NOD, HEAD_SHAKE

## ステップ 2: 歩容メソッドを実装する

```python
def _apply_my_new_motion(self) -> None:
    """Describe what this motion looks like visually."""
    self._gait_phase = (self._gait_phase + self.gait_speed) % 1.0
    phase_rad = self._gait_phase * 2.0 * math.pi

    # Option A: IK-based (preferred for walking/stepping)
    for leg_id in self.TRIPOD_A:
        dx = 10.0 * math.sin(phase_rad)           # mm forward/back
        dy = 0.0                                    # mm left/right
        dz = 5.0 * max(0, math.sin(phase_rad))    # mm up (lift)
        self._set_leg_from_ik(leg_id, dx, dy, dz)

    # Option B: Direct servo ticks (for simple oscillations)
    for leg_id in self.TRIPOD_B:
        offset = int(200 * math.sin(phase_rad))
        self._leg[f"leg_{leg_id}_pitch1"] = 2048 + offset
```

## ステップ 3: ディスパッチを接続する

`step()` メソッド内に追加します。

```python
elif self._motion == Motion.MY_NEW_MOTION:
    self._apply_my_new_motion()
```

## ステップ 4: API の対応付けを追加する

`robot.py` に追加します。

```python
_MOTION_MAP = {
    # ... existing ...
    "my_new_motion": Motion.MY_NEW_MOTION,
}

def my_new_motion(self) -> None:
    """Start my new motion."""
    self._engine.motion = Motion.MY_NEW_MOTION
```

## ステップ 5: LLM のツール呼び出しに公開する（palmimo_sdk.agent）

新しいファサードのモーションは、ツールを用意するまで LLM エージェントからは見えません。 `palmimo_sdk/agent/tools.py` では、`Tool` を直接継承せず、`Expressive` のサブクラスを追加します。これにより、他の移動・ジェスチャー・首のツールと同じオプションの `face` / `say` 引数を使えます（動きながら話したり表情を変えたりできます）。 `execute()` ではなく `_act()` を実装し、`_run_motion` のパターンに従います。モーションを設定 → `run(seconds=)` → 必ず `stop()` を実行、という流れです。 `long_running: ClassVar[bool] = True` を設定します。時間指定のブロッキングモーションは、呼び出し側が `Palmimo.cancel()`/`MotionCancelled` による割り込みと競合させる可能性があります（`robot.cancel()` を参照）。`Tool` 基底クラスの既定値 `False` のままにするのは、内部でブロッキングする `run()` をまったく呼ばないツール（`set_face`/`show_emoji`/ `say`/`capture` 型の呼び出し）か、`run()` を呼んでも意図的に対象外とするツールだけです（`tools.py` の `stop` 自身のコメントを参照）。その後、 `palmimo_sdk/agent/toolset.py` の `TOOL_MODELS` にツールを登録します（SDK に含めるツールの唯一の正本です）。対応するスキーマとディスパッチのテストを `packages/palmimo_sdk/tests/test_agent_tools.py` に追加します。

`long_running=True` は、`_run_motion` 経由で動く時間指定の移動・ジェスチャーツールだけのものではありません。`look`/`look_center` も `long_running=True` です。これらは `robot.run(seconds=_NECK_SETTLE_SECONDS)` でブロックし、首の目標値を実際にドライバーへ送り続けるためです（目標値を設定するだけでは何も動きません）。`Tool` 基底クラスの既定値 `False` のままにするツールは、内部にブロッキングする `run()` がなく即座に完了するもの（`set_face`、 `show_emoji`、`say`、`capture`）か、`stop` のように `run()` を呼んでも意図的に対象外とするものだけです。`stop()` は、実行中の動作を止めるためにエージェントが使うツールそのものなので、`stop()` をキャンセルと競合させる意味がありません。

モーションを意図的に LLM 用に公開しない場合に限り、このステップを省略します。その場合は `toolset.py` にコメントを残し、見落としではなく判断による省略だと分かるようにしてください。

## ステップ 6: テストする

### 自動回帰テスト

ハードウェアで動かす前に、`packages/palmimo_sdk/tests/` にテストを追加または更新します。 [CONTRIBUTING.md](../../../CONTRIBUTING.md#running-the-checks) の SDK テストコマンドを使います。

最低限、モーション全体を完了まで動かし、安全な tick 範囲、ニュートラルへの滑らかな復帰、終盤の安定性、左右両方のバリエーション、公開するすべての調整項目の境界値を検証します。決定的な受け入れ基準は、使い捨ての検証スクリプトではなく pytest に置きます。

### ドライラン

ドライバーを接続せずに Python API から新しいモーションを動かし（[Python API を試す](../../../README.ja.md#python-api-を試すハードウェア不要)を参照）、 `step()` の出力を調べます。

### 安全性の検証
- 脚を手として持ち上げる新しいジェスチャーは、 `Palmimo._wave_arm_legs()` に追加する必要があります。これにより、過負荷ガードがその脚を腕用のしきい値で監視し、ジェスチャーの調整でも腕として扱います。追加しない場合、その脚はより高い脚用のしきい値で判定されます
- すべてのサーボ位置が 0〜4095 の範囲内であること
- 極端な値（200 未満または 3900 超）がないこと
- ニュートラル → モーション → ニュートラルの遷移が滑らかであること
- 連続するフレーム間で急な飛びがないこと

### 実機テスト

ドライランとハードウェアを浮かせた状態での検証に合格したら、接続済みの `DynamixelDriver` を持つ `Palmimo` インスタンスを通じて実機でモーションを動かします（[周辺機器と接続](../reference/api-reference.md#周辺機器と接続)を参照）。
- 見た目が意図したモーションになっていること
- サーボから異音（こすれる音、カチカチ音）がしないこと
- ロボットが安定していること（転倒しないこと）
- `stop()` で滑らかにニュートラルへ戻ること

バランス、騒音、追従の遅れ、温度など、主観的な性質や物理的な性質は pytest では判断できません。ロボットを地面から浮かせ、実機でモーションを動かして確かめてください。

## 構成要素のリファレンス

| ツール | 用途 | 注記 |
|------|-------|-------|
| `self._set_leg_from_ik(leg_id, dx, dy, dz)` | mm 単位の足先位置 | 歩行に推奨 |
| `self._leg[f"leg_{id}_yaw"]` | サーボの tick を直接指定 | 中心 = 2048 |
| `self._leg[f"leg_{id}_pitch1"]` | 脚の上側関節の tick | 中心 = 2048 |
| `self._leg[f"leg_{id}_pitch2"]` | 脚の下側関節の tick | 中心 = 2048 |
| `self._gait_phase` | 0.0〜1.0 の周期 | 周期的なモーションに使用 |
| `self.gait_speed` | ステップごとの位相増分 | モーションの速度を制御 |
| `TRIPOD_A` / `TRIPOD_B` | [1,3,5] / [2,4,6] | 交互に動く歩容グループ |
| `LEFT_LEGS` / `RIGHT_LEGS` | [1,2,3] / [4,5,6] | 左右のグループ |
| `self.NEUTRAL` | 2048 | 中心位置の定数 |

## 振り付け

テストが済んだら、振り付けのシーケンスにモーションを追加します。

```python
routine = [
    ("forward", 2.0),
    ("my_new_motion", 3.0),
    ("idle", 1.0),
]
for pos in robot.play(routine, fps=60):
    send_to_hardware(pos)
```
