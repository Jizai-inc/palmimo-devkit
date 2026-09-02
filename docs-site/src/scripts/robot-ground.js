// Standing a posed model on the ground, and reading where it walked to. Pure
// vector math: it takes foot positions and knows nothing about joints, servos
// or three.js scenes beyond the math types, so it is testable the way
// robot-pose.js is.
import { Euler, Quaternion, Vector3 } from 'three';

// How far above the most loaded foot a foot has to rise before it stops
// counting as carrying weight, in model units (metres). A swing leg lifts far
// more than this, a stance leg far less, and the falloff is smooth on purpose:
// choosing a stance set by a threshold would make the fit jump the moment a
// foot crossed it, which is exactly the pop this is shaped to avoid.
const CARRY_FALLOFF = 0.005;
// Below this the weighted feet are collinear or coincident and the tilt is
// noise rather than a direction, so only the lift is taken.
const SINGULAR = 1e-18;

const UP = new Vector3(0, 1, 0);

/**
 * How much of the robot's weight each foot is carrying, from 1 down towards 0.
 *
 * Measured against where that foot rested rather than against a level plane,
 * because the exported model's own six feet sit up to 11 mm apart in height:
 * it is a CAD rest pose, not a stance settled onto a floor.
 *
 * @param {import('three').Vector3[]} feet Contact points now, in model space.
 * @param {import('three').Vector3[]} restFeet The same points in the rest pose.
 * @returns {number[]}
 */
export function carryWeights(feet, restFeet) {
  const risen = feet.map((foot, i) => foot.y - restFeet[i].y);
  const lowest = Math.min(...risen);
  return risen.map((rise) => Math.exp(-(rise - lowest) / CARRY_FALLOFF));
}

/**
 * The transform that keeps a posed model's loaded feet at the height they rested.
 *
 * Returns the roll, pitch and lift of the body that cancels the vertical
 * movement of the feet still carrying weight. The others -- a swing leg
 * mid-stride -- fall out of the fit by weight rather than by being excluded, so
 * nothing in the result steps as the gait hands over from one tripod to the next.
 *
 * This is the part of the pose the rest pose can anchor. Where the robot has
 * walked to has no such anchor and is accumulated instead, by stepBetween.
 *
 * @param {import('three').Vector3[]} feet Contact points now, in model space.
 * @param {import('three').Vector3[]} restFeet The same points in the rest pose.
 * @returns {{quaternion: import('three').Quaternion, position: import('three').Vector3}}
 */
export function standOnFeet(feet, restFeet) {
  requireMatchedFeet('standOnFeet', feet, restFeet);

  const risen = feet.map((foot, i) => foot.y - restFeet[i].y);
  const weights = carryWeights(feet, restFeet);

  // Body lift L, roll about +X by a and pitch about +Z by g move foot i
  // vertically by L + g*x - a*z, to first order. Fit those three to undo the
  // rise, in the rest pose's own coordinates so the gait cannot move the
  // basis it is being fitted against.
  const m = [0, 0, 0, 0, 0, 0, 0, 0, 0];
  const rhs = [0, 0, 0];
  restFeet.forEach((rest, i) => {
    const basis = [1, rest.x, -rest.z];
    const w = weights[i];
    for (let r = 0; r < 3; r += 1) {
      rhs[r] += w * basis[r] * -risen[i];
      for (let c = 0; c < 3; c += 1) {
        m[r * 3 + c] += w * basis[r] * basis[c];
      }
    }
  });

  const solved = solve3(m, rhs);
  if (solved === null) {
    const total = weights.reduce((sum, w) => sum + w, 0);
    const lift = -weights.reduce((sum, w, i) => sum + w * risen[i], 0) / total;
    return { quaternion: new Quaternion(), position: new Vector3(0, lift, 0) };
  }

  const [lift, pitch, roll] = solved;
  return {
    quaternion: new Quaternion().setFromEuler(new Euler(roll, 0, pitch, 'XYZ')),
    position: new Vector3(0, lift, 0),
  };
}

/**
 * How far the body travelled between two frames, read off its planted feet.
 *
 * A foot on the ground does not move, so its travel backwards through the body
 * is the body's travel forwards through the world. Fitting the rigid motion
 * that best carries this frame's loaded feet back onto the previous frame's
 * gives that travel directly -- no odometry from anywhere else, and no model of
 * the gait: a stride the SDK changes shows up here as a different answer.
 *
 * Each foot is weighted by how loaded it is in *both* frames, so a foot landing
 * or leaving joins and leaves the fit smoothly rather than at a threshold.
 *
 * The fit is flat: it takes the feet's x and z and returns a turn and a shift.
 * Roll and pitch tilt those readings by a cosine, which at the few degrees this
 * robot leans is a part in a thousand -- and keeping the two fits independent
 * matters more, because a horizontal fit that chased the tilt would feed the
 * tilt's own error back into where the robot thinks it is.
 *
 * @param {import('three').Vector3[]} feet Contact points now, in model space.
 * @param {import('three').Vector3[]} prevFeet The same points one frame ago.
 * @param {import('three').Vector3[]} restFeet The same points in the rest pose.
 * @returns {{turn: number, shift: import('three').Vector3}} In the body's frame.
 */
export function stepBetween(feet, prevFeet, restFeet) {
  requireMatchedFeet('stepBetween', feet, restFeet);
  requireMatchedFeet('stepBetween', prevFeet, restFeet);

  const now = carryWeights(feet, restFeet);
  const before = carryWeights(prevFeet, restFeet);
  const weights = now.map((weight, i) => weight * before[i]);
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  if (total < SINGULAR) {
    return { turn: 0, shift: new Vector3() };
  }

  const here = weightedCentre(feet, weights, total);
  const there = weightedCentre(prevFeet, weights, total);

  // The turn that best lines this frame's spread of feet up with the last
  // frame's: the angle maximising the weighted dot product of the two, which is
  // the flat case of Kabsch's solution.
  let along = 0;
  let across = 0;
  feet.forEach((foot, i) => {
    const ax = foot.x - here.x;
    const az = foot.z - here.z;
    const bx = prevFeet[i].x - there.x;
    const bz = prevFeet[i].z - there.z;
    along += weights[i] * (ax * bx + az * bz);
    across += weights[i] * (az * bx - ax * bz);
  });
  const turn = Math.atan2(across, along);

  return { turn, shift: there.sub(here.applyAxisAngle(UP, turn)) };
}

/**
 * Compose one body-frame step onto the travel accumulated so far.
 *
 * @param {{turn: number, position: import('three').Vector3}} travel
 * @param {{turn: number, shift: import('three').Vector3}} step
 * @returns {{turn: number, position: import('three').Vector3}}
 */
export function advanceTravel(travel, step) {
  return {
    turn: travel.turn + step.turn,
    // The step is measured in the body's frame, so it has to be turned into the
    // world by the heading the body had when it took it.
    position: travel.position.clone().add(step.shift.clone().applyAxisAngle(UP, travel.turn)),
  };
}

/** The weighted mean of some feet, flattened onto the ground plane. */
function weightedCentre(feet, weights, total) {
  const centre = new Vector3();
  feet.forEach((foot, i) => {
    centre.x += weights[i] * foot.x;
    centre.z += weights[i] * foot.z;
  });
  return centre.divideScalar(total);
}

function requireMatchedFeet(caller, feet, restFeet) {
  if (feet.length !== restFeet.length || feet.length < 3) {
    // A model that cannot say where it touches the ground has drifted from
    // this code the same way an unknown joint name has drifted from
    // poseJoints -- both fail loudly rather than guess.
    throw new Error(`${caller} needs at least 3 matched feet, got ${feet.length}/${restFeet.length}`);
  }
}

/** Solve a 3x3 system by Cramer's rule; null when it is singular. */
function solve3(m, rhs) {
  const det = determinant3(m);
  if (Math.abs(det) < SINGULAR) {
    return null;
  }
  return [0, 1, 2].map((column) => {
    const swapped = [...m];
    for (let row = 0; row < 3; row += 1) {
      swapped[row * 3 + column] = rhs[row];
    }
    return determinant3(swapped) / det;
  });
}

function determinant3(m) {
  return (
    m[0] * (m[4] * m[8] - m[5] * m[7]) -
    m[1] * (m[3] * m[8] - m[5] * m[6]) +
    m[2] * (m[3] * m[7] - m[4] * m[6])
  );
}
