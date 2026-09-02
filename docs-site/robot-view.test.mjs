import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { Raycaster, Vector3 } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { SCREEN_ACROSS, SCREEN_STANDOFF } from './src/scripts/robot-face.js';
import { headAnchor } from './src/scripts/robot-head.js';
import { poseJoints, readJoints } from './src/scripts/robot-pose.js';
import {
  DECLINATION,
  FRAME_HEIGHT,
  FRAME_WIDTH,
  LENS_ACROSS,
  bgrFromReadback,
  insetViewport,
  mountView,
} from './src/scripts/robot-view.js';

// Read against the real asset rather than a fixture: what this file is for is
// whether the camera was put on the right node of the model that ships, and a
// stand-in model would agree with whatever mistake the code made.
const MODEL_URL = new URL('./public/models/palmimo.glb', import.meta.url);
const UP = new Vector3(0, 1, 0);
const FORWARD = new Vector3(1, 0, 0);

async function loadModel() {
  const file = readFileSync(MODEL_URL);
  const { scene } = await new GLTFLoader().parseAsync(
    file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength),
    ''
  );
  return scene;
}

/** Up the face rather than up the world: the head is tilted back. */
function inPlaneUp(anchor) {
  return anchor.up.clone().addScaledVector(anchor.facing, -anchor.up.dot(anchor.facing)).normalize();
}

/**
 * A way to ask whether the head is open at a point on its face.
 *
 * The head is one closed shell with no boundary edges, so its openings are
 * gaps between parts and cannot be read off the topology. What makes one an
 * opening is that a ray sent at the face from well in front of it comes out
 * the other side.
 *
 * @param {{head: import('three').Object3D, facing: import('three').Vector3}} anchor
 * @param {boolean} withLens Whether the stage's own lens counts as head. False
 *   asks where the asset is open; true asks what a viewer can still see through.
 * @returns {(spot: import('three').Vector3) => boolean} Taking a point in head frame.
 */
function seesThrough(anchor, withLens = false) {
  const meshes = [];
  anchor.head.traverse((child) => {
    if (!child.isMesh) {
      return;
    }
    for (let node = child; !withLens && node !== null && node !== anchor.head; node = node.parent) {
      if (node.name === 'camera_lens') {
        return;
      }
    }
    meshes.push(child);
  });
  anchor.head.updateMatrixWorld(true);
  const toWorld = anchor.head.matrixWorld;
  const facing = anchor.facing.clone().transformDirection(toWorld).normalize();
  const caster = new Raycaster();
  return (spot) => {
    const from = spot.clone().applyMatrix4(toWorld).addScaledVector(facing, 0.3);
    caster.set(from, facing.clone().negate());
    return caster.intersectObjects(meshes, false).length === 0;
  };
}

/** Where the camera is pointing, in the model's own frame. */
function aim(model, camera) {
  model.updateMatrixWorld(true);
  return new Vector3(0, 0, -1).transformDirection(camera.matrixWorld).normalize();
}

test('the robot looks where it walks', async () => {
  const model = await loadModel();
  const { camera } = mountView(model, readJoints(model));

  // `forward()` carries the body along +x, so at rest that is what the head
  // is facing. A camera hung on the head backwards points at the robot.
  const flat = aim(model, camera).setY(0).normalize();
  assert.ok(flat.angleTo(FORWARD) < 0.02, `aimed ${flat.toArray()}`);
});

test('the lens is set below level, so it holds ground and not just horizon', async () => {
  const model = await loadModel();
  const { camera } = mountView(model, readJoints(model));

  // Dead level, this camera sees the fog and nothing else: its axis runs
  // parallel to the ground it is 293 mm above and never meets it.
  const below = Math.asin(-aim(model, camera).y);
  assert.ok(Math.abs(below - DECLINATION) < 0.01, `set ${below} below level, not ${DECLINATION}`);
});

test('the lens sits at the front of the head, not inside the body', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera } = mountView(model, joints);
  const anchor = headAnchor(model, joints);
  model.updateMatrixWorld(true);

  const eye = new Vector3().setFromMatrixPosition(camera.matrixWorld);
  const neck = new Vector3().setFromMatrixPosition(anchor.head.matrixWorld);
  // Out at the face, and up in the air rather than down among the feet. A
  // camera left at the node's own origin fails the first: it sits back inside
  // the shell, and the frame it returns is the inside of the robot's head.
  assert.ok(eye.x - neck.x > 0.05, `only ${eye.x - neck.x} m ahead of the neck`);
  assert.ok(eye.y > 0.1, `only ${eye.y} m up`);
});

test('the lens is clear of the face, which shares the head face with it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera } = mountView(model, joints);
  const anchor = headAnchor(model, joints);

  // Both stand off the head's face along the way it looks, so how far each
  // stands off is the whole comparison. A lens level with the screen, or
  // behind it, has the back of the screen filling the frame the moment the
  // near plane is brought in.
  const lens = camera.position.clone().sub(anchor.eye).dot(anchor.facing);
  const screen = anchor.across * SCREEN_STANDOFF;
  assert.ok(lens > screen, `the lens sits at ${lens} m against the screen's ${screen} m`);
});

test('the camera looks out below the screen rather than through it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera } = mountView(model, joints);
  const anchor = headAnchor(model, joints);

  const offset = camera.position.clone().sub(anchor.eye);
  const onFace = offset.addScaledVector(anchor.facing, -offset.dot(anchor.facing));
  const below = -onFace.dot(inPlaneUp(anchor));

  // Clear of the screen laid on the plate above it, and still on the head.
  assert.ok(below > (anchor.across / 2) * SCREEN_ACROSS, `only ${below} m below the middle of the face`);
  assert.ok(below < anchor.across, `${below} m below the middle, which is off the head`);
  // Straight down the face, not off to one side of it.
  assert.ok(Math.abs(onFace.length() - below) < 1e-9, 'the camera is set sideways across the face');
});

test('the lens fills the opening the shell cuts for the camera', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  mountView(model, joints);
  const anchor = headAnchor(model, joints);
  const lens = anchor.head.getObjectByName('camera_lens');
  const sees = seesThrough(anchor);

  const radius = (anchor.across * LENS_ACROSS) / 2;
  const middle = lens.position
    .clone()
    .addScaledVector(anchor.facing, -lens.position.clone().sub(anchor.eye).dot(anchor.facing));
  const up = inPlaneUp(anchor);
  const right = new Vector3().crossVectors(up, anchor.facing).normalize();
  const around = (turn, out) =>
    middle
      .clone()
      .addScaledVector(right, Math.cos(turn) * out)
      .addScaledVector(up, Math.sin(turn) * out);

  // The head is a closed shell, so a lens on it is a decal unless it is over
  // one of the few places the shell is actually cut through. Inside its rim
  // the model has to be open all the way through...
  assert.ok(sees(middle), 'the lens is stuck on solid shell, not over an opening');
  const turns = Array.from({ length: 24 }, (unused, i) => (i / 24) * Math.PI * 2);
  assert.deepEqual(
    turns.filter((turn) => !sees(around(turn, radius * 0.95))),
    []
  );
  // ...and just outside it solid, or the lens is not filling the hole -- it is
  // floating in the open air beside the head, which would pass everything
  // above.
  const held = turns.filter((turn) => !sees(around(turn, radius * 1.4)));
  assert.ok(held.length > turns.length * 0.7, `only ${held.length} of ${turns.length} sides of the lens are shell`);
});

test('a lens stands where the camera looks out, since the shell has no aperture', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera, dispose } = mountView(model, joints);
  const anchor = headAnchor(model, joints);

  const lens = anchor.head.getObjectByName('camera_lens');
  assert.ok(lens !== undefined, 'nothing on the head shows the reader where the camera is');
  // Clear of the face and still behind the camera: a lens the camera sits
  // inside of fills the frame with itself.
  const proud = lens.position.clone().sub(anchor.eye).dot(anchor.facing);
  const stands = camera.position.clone().sub(anchor.eye).dot(anchor.facing);
  assert.ok(proud > 0, `the lens sits ${proud} m into the shell`);
  assert.ok(proud < stands, `the lens sits at ${proud} m against the camera's ${stands} m`);

  dispose();
  assert.equal(anchor.head.getObjectByName('camera_lens'), undefined);
});

test('the lens blacks the window out, so orbiting the head finds no way through it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  mountView(model, joints);
  const anchor = headAnchor(model, joints);
  const lens = anchor.head.getObjectByName('camera_lens');
  const bare = seesThrough(anchor);
  const dressed = seesThrough(anchor, true);

  const up = inPlaneUp(anchor);
  const right = new Vector3().crossVectors(up, anchor.facing).normalize();
  const middle = lens.position
    .clone()
    .addScaledVector(anchor.facing, -lens.position.clone().sub(anchor.eye).dot(anchor.facing));

  // Every point of the window, found rather than written down: the window is
  // wherever the asset alone lets a ray through. A lens narrower than that,
  // or one with an open back, leaves the head see-through -- which from
  // behind the robot is a hole in its face.
  //
  // Swept over a disc rather than a square, because the bracket ends 14 mm
  // out and the open air past it is not a hole in anything.
  const reach = anchor.across * 0.095;
  const step = reach / 8;
  const leaks = [];
  for (let x = -reach; x <= reach; x += step) {
    for (let y = -reach; y <= reach; y += step) {
      if (Math.hypot(x, y) > reach) {
        continue;
      }
      const spot = middle.clone().addScaledVector(right, x).addScaledVector(up, y);
      if (bare(spot) && dressed(spot)) {
        leaks.push([x, y]);
      }
    }
  }
  assert.deepEqual(leaks, []);
});

test('turning the neck turns the view with it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera } = mountView(model, joints);
  const before = aim(model, camera);

  // The bug this guards: anchoring the camera above neck_yaw in the chain.
  // The head still turns, the view does not, and a head_shake shows nothing.
  poseJoints(joints, { neck_yaw: 0.35 });
  const turned = aim(model, camera).angleTo(before);

  assert.ok(Math.abs(turned - 0.35) < 0.02, `turned ${turned} for a 0.35 rad yaw`);
});

test('nodding the neck tips the view with it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const { camera } = mountView(model, joints);
  const before = aim(model, camera);

  poseJoints(joints, { neck_pitch1: 0.25 });
  const after = aim(model, camera);

  assert.ok(Math.abs(after.angleTo(before) - 0.25) < 0.02, `tipped ${after.angleTo(before)}`);
  // A pitch is not a yaw: the view stays in the plane it started in.
  assert.ok(Math.abs(after.clone().cross(before).normalize().dot(UP)) < 0.05, 'a nod turned the head sideways');
});

test('a readback comes back as a camera frame, not as what the GPU handed over', () => {
  // Two pixels: the bottom-left one red, the top-left one blue, as WebGL
  // stores them -- bottom row first, RGBA.
  const rgba = new Uint8Array([255, 0, 0, 255, 0, 0, 255, 255]);
  const bgr = bgrFromReadback(rgba, 1, 2);

  // OpenCV's order: top row first, blue first. So the blue pixel leads, and
  // reads as (255, 0, 0) once its channels are in BGR.
  assert.deepEqual([...bgr], [255, 0, 0, 0, 0, 255]);
});

test('a readback that is too small to be a frame is refused', () => {
  assert.throws(() => bgrFromReadback(new Uint8Array(4), FRAME_WIDTH, FRAME_HEIGHT), /needs \d+ bytes/);
});

test('the inset is placed against the bottom of the canvas, not the top', () => {
  // A 800x400 canvas with a 200x150 frame in its top-right corner, 10 from
  // each edge -- the numbers a page would give.
  const canvasBox = { left: 100, bottom: 500 };
  const frame = { left: 690, bottom: 260, width: 200, height: 150 };

  // The bug this guards: passing the page's own top-down y straight to the
  // renderer, which puts the inset at the bottom of the stage instead.
  assert.deepEqual(insetViewport(canvasBox, frame), { x: 590, y: 240, width: 200, height: 150 });
});

test('an inset frame the page has not revealed yet is not drawn into', () => {
  assert.equal(insetViewport({ left: 0, bottom: 400 }, { left: 0, bottom: 0, width: 0, height: 0 }), null);
});
