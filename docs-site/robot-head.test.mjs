import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { BufferGeometry, Float32BufferAttribute, Matrix4, Mesh, Vector3 } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { SCREEN_ACROSS } from './src/scripts/robot-face.js';
import { HEAD_NODE, headAnchor } from './src/scripts/robot-head.js';
import { readJoints } from './src/scripts/robot-pose.js';

// Read against the real asset: what is being checked is where the head of the
// model that ships carries its face plate, and a fixture would agree with
// whatever the code assumed.
const MODEL_URL = new URL('./public/models/palmimo.glb', import.meta.url);
// Three anchors that were shipped and looked wrong on the page, kept so the
// property below is known to reject them. Head frame, metres.
const REJECTED = {
  'the middle of the bounding box': new Vector3(-0.09, 0.0217, -0.0055),
  'the average of the forward-most corners': new Vector3(-0.09, -0.0244, -0.0206),
  'the middle of the dish in the head front': new Vector3(-0.0699, 0.0089, -0.0152),
};
// The plate is flat to well under this, and nothing else on the head is.
const IN_PLANE = 0.001;

async function loadModel() {
  const file = readFileSync(MODEL_URL);
  const { scene } = await new GLTFLoader().parseAsync(
    file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength),
    ''
  );
  return scene;
}

function headCorners(head) {
  const corners = [];
  head.traverse((child) => {
    if (!child.isMesh) {
      return;
    }
    const position = child.geometry.attributes.position;
    for (let i = 0; i < position.count; i += 1) {
      corners.push(new Vector3().fromBufferAttribute(position, i));
    }
  });
  return corners;
}

/**
 * How much of the head lies in the plane a screen at *eye* would occupy, and
 * how far around the screen that surface goes.
 *
 * A screen laid on the plate has the whole plate in its plane, all the way
 * round and out past its own edge. One hung in front of the head, or laid over
 * the dish behind the plate, catches only whatever the plane happens to clip.
 */
function surfaceUnder(corners, eye, normal, axes) {
  const sides = new Array(8).fill(false);
  const sideways = new Vector3();
  let held = 0;
  let reach = 0;
  for (const corner of corners) {
    sideways.subVectors(corner, eye);
    const ahead = sideways.dot(normal);
    if (Math.abs(ahead) > IN_PLANE) {
      continue;
    }
    sideways.addScaledVector(normal, -ahead);
    held += 1;
    reach = Math.max(reach, sideways.length());
    const angle = Math.atan2(sideways.dot(axes.over), sideways.dot(axes.left));
    sides[Math.floor(((angle + Math.PI) / (Math.PI * 2)) * 8) % 8] = true;
  }
  return { held, reach, sides: sides.filter(Boolean).length };
}

function axesAround(normal, up) {
  const left = new Vector3().crossVectors(up, normal).normalize();
  return { left, over: new Vector3().crossVectors(normal, left).normalize() };
}

test('the face lies on the head plate, which runs past it on every side', async () => {
  const model = await loadModel();
  const anchor = headAnchor(model, readJoints(model));
  const corners = headCorners(anchor.head);
  const radius = (anchor.across / 2) * SCREEN_ACROSS;

  const plate = surfaceUnder(corners, anchor.eye, anchor.facing, axesAround(anchor.facing, anchor.up));
  assert.ok(plate.held > 64, `only ${plate.held} corners in the screen plane`);
  assert.equal(plate.sides, 8);
  assert.ok(plate.reach > radius, `the plate stops at ${plate.reach} m, inside the screen`);

  for (const [what, eye] of Object.entries(REJECTED)) {
    const other = surfaceUnder(corners, eye, anchor.forward, axesAround(anchor.forward, anchor.up));
    assert.ok(
      other.held <= 64 || other.sides < 8,
      `${what} would now pass, so this no longer catches a face off the plate`
    );
  }
});

test('nothing on the head stands in front of the face', async () => {
  const model = await loadModel();
  const anchor = headAnchor(model, readJoints(model));
  const radius = (anchor.across / 2) * SCREEN_ACROSS;

  const sideways = new Vector3();
  const blocking = headCorners(anchor.head).filter((corner) => {
    sideways.subVectors(corner, anchor.eye);
    const ahead = sideways.dot(anchor.facing);
    return ahead > IN_PLANE && sideways.addScaledVector(anchor.facing, -ahead).length() < radius;
  });
  assert.deepEqual(blocking, []);
});

test('the mouth is a feature of the head, and at neutral it looks where the robot walks', async () => {
  const model = await loadModel();
  const anchor = headAnchor(model, readJoints(model));

  const head = new Vector3(-Infinity, -Infinity, -Infinity);
  const low = new Vector3(Infinity, Infinity, Infinity);
  for (const corner of headCorners(anchor.head)) {
    low.min(corner);
    head.max(corner);
  }
  const narrowest = Math.min(...head.sub(low).toArray());

  // The mouth is most of the head, because the head is a bowl -- but it is
  // still a feature of the head and not the whole outline of it.
  assert.ok(anchor.across > narrowest / 2, `${anchor.across} m across a ${narrowest} m head`);
  assert.ok(anchor.across < narrowest, `${anchor.across} m across a ${narrowest} m head`);
  // At neutral the face looks the way the robot walks. It does not in the
  // export, which stands the head a tenth of pi across the body -- see
  // REST_CORRECTION in robot-pose.js -- and a plate found on the side or the
  // back of the head would not either.
  const turn = anchor.facing.angleTo(anchor.forward);
  assert.ok(turn < 0.02, `the face looks ${turn} rad off the walk with the neck at neutral`);
});

/**
 * A lens barrel of the shape the stage could plausibly grow: a disc with a
 * wall running back from its rim, welded so the two meet at a crease. That rim
 * is a perfectly flat ring of many corners looking the way the head looks,
 * which beats the real plate on flatness -- so a head measured with the barrel
 * still attached wears its face on its own lens.
 *
 * Welded by hand because three.js primitives are not: `CylinderGeometry` gives
 * its cap and its wall separate corners, so they share no edge and raise no
 * crease. A barrel loaded from a file would.
 */
function furnitureOn(anchor, dressed) {
  const sides = 48;
  const radius = 0.03;
  const depth = 0.012;
  const points = [0, 0, 0];
  const faces = [];
  for (let i = 0; i < sides; i += 1) {
    const turn = (i / sides) * Math.PI * 2;
    points.push(Math.cos(turn) * radius, Math.sin(turn) * radius, 0);
  }
  for (let i = 0; i < sides; i += 1) {
    const turn = (i / sides) * Math.PI * 2;
    points.push(Math.cos(turn) * radius, Math.sin(turn) * radius, -depth);
  }
  const rim = (i) => 1 + (i % sides);
  const back = (i) => 1 + sides + (i % sides);
  for (let i = 0; i < sides; i += 1) {
    faces.push(0, rim(i), rim(i + 1));
    faces.push(rim(i), back(i), back(i + 1));
    faces.push(rim(i), back(i + 1), rim(i + 1));
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(points, 3));
  geometry.setIndex(faces);

  const barrel = new Mesh(geometry);
  barrel.userData.stageDressing = dressed;
  barrel.position.copy(anchor.eye).addScaledVector(anchor.facing, 0.012);
  barrel.quaternion.setFromRotationMatrix(
    new Matrix4().lookAt(new Vector3(), anchor.facing.clone().negate(), anchor.up)
  );
  anchor.head.add(barrel);
}

test('the head is measured without the furniture the stage hangs on it', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const bare = headAnchor(model, joints);

  furnitureOn(bare, true);
  const dressed = headAnchor(model, joints);

  assert.ok(dressed.eye.distanceTo(bare.eye) < 1e-9, `the face moved ${dressed.eye.distanceTo(bare.eye)} m`);
  assert.ok(dressed.facing.angleTo(bare.facing) < 1e-9, 'the face turned');
  assert.equal(dressed.across, bare.across);
});

test('unmarked furniture would take the face over, which is why it is marked', async () => {
  const model = await loadModel();
  const joints = readJoints(model);
  const bare = headAnchor(model, joints);

  furnitureOn(bare, false);
  const taken = headAnchor(model, joints);

  // Not a wish for this behaviour -- the guard above is only worth its lines
  // while this is what happens without it.
  assert.ok(taken.eye.distanceTo(bare.eye) > 0.005, 'a bare barrel no longer wins, so the guard is untested');
});

test('the head is the last neck joint, so every neck gesture carries the face', async () => {
  const model = await loadModel();
  const joints = readJoints(model);

  assert.ok(joints.has(HEAD_NODE));
  const below = [...joints.values()].filter((joint) => {
    let node = joint.node.parent;
    while (node !== null) {
      if (node === joints.get(HEAD_NODE).node) {
        return true;
      }
      node = node.parent;
    }
    return false;
  });
  assert.deepEqual(below, []);
});
