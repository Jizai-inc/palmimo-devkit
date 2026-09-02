import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Euler, Object3D, Quaternion, Vector3 } from 'three';

import { poseJoints, readJoints } from './src/scripts/robot-pose.js';

// A joint's exported rotation is what puts its axis where the robot's frame
// wants it, so no node of interest arrives at the identity. Posing has to be
// tested against a rest orientation that would show a mistake.
const REST = new Quaternion().setFromEuler(new Euler(0.3, -0.7, 1.1));
const Z = new Vector3(0, 0, 1);
// `angleTo` takes an acos, which amplifies rounding as the angle approaches
// zero: two quaternions built by the same arithmetic still read ~3e-8 apart.
const SAME = 1e-6;

function modelWithOneJoint() {
  const root = new Object3D();
  const joint = new Object3D();
  joint.name = 'leg_1_yaw';
  joint.quaternion.copy(REST);
  root.add(joint);
  return { root, joint };
}

test('posing turns a joint from its rest orientation rather than replacing it', () => {
  const { root, joint } = modelWithOneJoint();
  poseJoints(readJoints(root), { leg_1_yaw: 0.4 });

  const expected = REST.clone().multiply(new Quaternion().setFromAxisAngle(Z, 0.4));
  assert.ok(joint.quaternion.angleTo(expected) < SAME);
  // The failure this guards against: writing the turn straight onto the node,
  // which looks right for a joint that happened to rest at the identity.
  assert.ok(joint.quaternion.angleTo(new Quaternion().setFromAxisAngle(Z, 0.4)) > 1e-3);
});

test('posing to the same angle twice does not accumulate', () => {
  const { root, joint } = modelWithOneJoint();
  const joints = readJoints(root);

  poseJoints(joints, { leg_1_yaw: 0.4 });
  const once = joint.quaternion.clone();
  poseJoints(joints, { leg_1_yaw: 0.4 });

  assert.ok(joint.quaternion.angleTo(once) < SAME);
});

test('posing a joint the model does not have throws', () => {
  const { root } = modelWithOneJoint();
  assert.throws(() => poseJoints(readJoints(root), { leg_9_yaw: 0.4 }), /no joint named "leg_9_yaw"/);
});
