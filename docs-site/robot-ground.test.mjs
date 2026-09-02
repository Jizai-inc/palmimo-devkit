import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Vector3 } from 'three';

import { advanceTravel, standOnFeet, stepBetween } from './src/scripts/robot-ground.js';

// Six feet around the body, at the height the exported model rests them --
// deliberately not all equal, because the real asset's are not either, and a
// fit that only works on a level rest pose would pass a levelled fixture.
const REST = [
  [-0.119, -0.1595, -0.0796],
  [-0.0067, -0.1687, -0.1255],
  [0.1194, -0.1595, -0.0788],
  [-0.1194, -0.1595, 0.0788],
  [-0.0061, -0.1576, 0.1408],
  [0.1188, -0.1595, 0.0796],
].map(([x, y, z]) => new Vector3(x, y, z));

const NOTHING = 1e-9;

/** The rest feet, each moved by however much that leg extended. */
function pressed(byLeg) {
  return REST.map((foot, i) => new Vector3(foot.x, foot.y + byLeg[i], foot.z));
}

test('a model standing where it rested is not corrected at all', () => {
  // The bug this guards: fitting a level plane through feet that rest 11 mm
  // apart rocked the robot by 5 mm and 5 degrees a frame while it stood still.
  const { quaternion, position } = standOnFeet(pressed([0, 0, 0, 0, 0, 0]), REST);

  assert.ok(position.length() < NOTHING, `moved ${position.length()}`);
  assert.ok(quaternion.angleTo(quaternion.clone().identity()) < NOTHING);
});

test('legs pressing down together lift the body by what they extended', () => {
  const { quaternion, position } = standOnFeet(pressed(Array(6).fill(-0.01)), REST);

  assert.ok(Math.abs(position.y - 0.01) < 1e-6, `lifted ${position.y}`);
  assert.ok(Math.abs(position.x) < NOTHING && Math.abs(position.z) < NOTHING);
  // A push-up is not a tilt: the body goes straight up.
  assert.ok(quaternion.angleTo(quaternion.clone().identity()) < 1e-6);
});

test('legs pressing further the further out they are tilt the body', () => {
  // A press that varies linearly across the body is exactly what a tilt of the
  // body looks like from the feet, so the fit has to reproduce it exactly --
  // whatever weight each foot ends up carrying.
  const feet = pressed(REST.map((foot) => -0.005 - 0.02 * foot.x));
  const { quaternion, position } = standOnFeet(feet, REST);

  // Every foot should come back to the height it rested at, which is the whole
  // point of the fit -- checked on the transform rather than on its parts.
  for (const [i, foot] of feet.entries()) {
    const settled = foot.clone().applyQuaternion(quaternion).add(position);
    assert.ok(Math.abs(settled.y - REST[i].y) < 1e-4, `foot ${i} landed ${settled.y - REST[i].y} off`);
  }
  assert.ok(quaternion.angleTo(quaternion.clone().identity()) > 1e-3, 'expected a visible tilt');
});

test('a foot lifted clear of the ground does not drag the fit', () => {
  const planted = Array(6).fill(-0.01);
  const swinging = [...planted];
  swinging[2] = 0.04;

  const { position } = standOnFeet(pressed(swinging), REST);

  // The swing leg is carrying nothing, so the body rides on the other five at
  // the height they set -- the same height as if it were not there.
  assert.ok(Math.abs(position.y - 0.01) < 1e-3, `lifted ${position.y}`);
});

/** The rest feet, moved bodily through the body's frame and turned about it. */
function carried(shift, turn) {
  return REST.map((foot) =>
    new Vector3(foot.x, foot.y, foot.z).applyAxisAngle(new Vector3(0, 1, 0), turn).add(shift)
  );
}

test('feet that have not moved report no travel', () => {
  const { turn, shift } = stepBetween(REST, REST, REST);

  // Exactly zero, not nearly: a robot standing still that creeps a micron a
  // frame has walked a metre by the time a reader looks away and back.
  assert.equal(turn, 0);
  assert.equal(shift.length(), 0);
});

test('feet sliding backwards through the body carry the body forwards', () => {
  const back = new Vector3(-0.002, 0, 0);
  const { turn, shift } = stepBetween(carried(back, 0), REST, REST);

  assert.ok(Math.abs(shift.x - 0.002) < 1e-9, `travelled ${shift.x}`);
  assert.ok(Math.abs(shift.z) < 1e-9 && Math.abs(turn) < 1e-9);
});

test('feet swinging round the body turn the body the other way', () => {
  const { turn, shift } = stepBetween(carried(new Vector3(), -0.01), REST, REST);

  assert.ok(Math.abs(turn - 0.01) < 1e-9, `turned ${turn}`);
  // A turn about the feet's own centre is not also a step sideways.
  assert.ok(shift.length() < 1e-9, `drifted ${shift.length()}`);
});

test('a swing leg is left out of the travel it did not carry', () => {
  const back = new Vector3(-0.002, 0, 0);
  const stance = carried(back, 0);
  // One foot lifted and thrown forwards, the way a leg mid-stride is.
  stance[2] = new Vector3(REST[2].x + 0.03, REST[2].y + 0.04, REST[2].z);

  const { turn, shift } = stepBetween(stance, REST, REST);

  assert.ok(Math.abs(shift.x - 0.002) < 1e-4, `travelled ${shift.x}`);
  assert.ok(Math.abs(turn) < 1e-4, `turned ${turn}`);
});

test('steps compose in the heading the body had when it took them', () => {
  const forward = { turn: 0, shift: new Vector3(0.01, 0, 0) };
  let travel = { turn: 0, position: new Vector3() };

  travel = advanceTravel(travel, forward);
  travel = advanceTravel(travel, { turn: Math.PI / 2, shift: new Vector3(0.01, 0, 0) });
  travel = advanceTravel(travel, forward);

  // Two steps along +x, a quarter turn, then one more step -- which by then
  // points along -z, because the step is in the body's frame, not the world's.
  assert.ok(Math.abs(travel.turn - Math.PI / 2) < 1e-9);
  assert.ok(travel.position.distanceTo(new Vector3(0.02, 0, -0.01)) < 1e-9, `at ${travel.position.toArray()}`);
});
