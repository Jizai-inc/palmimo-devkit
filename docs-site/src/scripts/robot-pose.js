// Turning the robot model's joints. Pure scene-graph work: it takes angles in
// radians and knows nothing about servos, ticks or gaits -- the SDK owns that
// conversion, and this file must never grow a copy of it.
import { Quaternion, Vector3 } from 'three';

// Every driven node was exported with its joint's axis on local +Z, so one
// servo is one rotation about this vector -- a property of the provided model
// asset, described in this site's README.
const JOINT_AXIS = new Vector3(0, 0, 1);

// Where the export's rest pose is not the robot's neutral, in radians about
// each joint's own axis.
//
// The head arrives yawed a tenth of a turn of pi off the way the robot walks,
// while the six legs are exactly symmetric about that direction -- so it is
// the head that is off, not the body, and it is off by an angle the yaw servo
// could have been holding. Left alone, a robot asked for nothing stands
// looking past you.
//
// This belongs in the asset. Until the model is exported square it is undone
// here, so that neutral means straight ahead for everything downstream.
const REST_CORRECTION = { neck_yaw: Math.PI / 10 };

/**
 * Index a loaded model's nodes by name, keeping the orientation each arrived in.
 *
 * The rest orientation matters: a node's exported rotation places the joint's
 * axis, so posing has to turn *from* it rather than replace it. A joint listed
 * in `REST_CORRECTION` is turned to its true neutral first, and the model is
 * left standing in it.
 *
 * @param {import('three').Object3D} root
 * @returns {Map<string, {node: import('three').Object3D, rest: Quaternion}>}
 */
export function readJoints(root) {
  const joints = new Map();
  const turn = new Quaternion();
  root.traverse((node) => {
    if (node.name === '') {
      return;
    }
    // Own keys only: this runs over every named node in the file, and a node
    // named for something on Object's prototype would otherwise resolve to a
    // function and turn the joint by NaN.
    if (Object.hasOwn(REST_CORRECTION, node.name) && node.userData.corrected !== true) {
      node.quaternion.multiply(turn.setFromAxisAngle(JOINT_AXIS, REST_CORRECTION[node.name]));
      // A flag rather than the orientation it replaced: three.js puts
      // `userData` through JSON when a model is cloned, which survives a
      // boolean and would hand back a Quaternion with no methods on it.
      node.userData.corrected = true;
    }
    joints.set(node.name, { node, rest: node.quaternion.clone() });
  });
  return joints;
}

/**
 * Turn named joints to the given angles, in radians, from the rest pose.
 *
 * Unknown names throw rather than being skipped: the names come from the robot
 * definition both sides were built from, so one that does not resolve means the
 * model and the caller have drifted apart, which is worth failing loudly for.
 *
 * @param {Map<string, {node: import('three').Object3D, rest: Quaternion}>} joints
 * @param {Record<string, number>} angles
 */
export function poseJoints(joints, angles) {
  const turn = new Quaternion();
  for (const [name, radians] of Object.entries(angles)) {
    const joint = joints.get(name);
    if (joint === undefined) {
      throw new Error(`the model has no joint named "${name}"`);
    }
    turn.setFromAxisAngle(JOINT_AXIS, radians);
    joint.node.quaternion.copy(joint.rest).multiply(turn);
  }
}
