// Where the robot's head is, and where its face plate is.
//
// Two things hang off the head and both need the same answer: the camera that
// looks out of it and the display that looks back at you. Measured off the
// asset at rest rather than written down, so a re-export that reframes the
// head carries them both with it.
import { Matrix4, Vector3 } from 'three';

// The last link in the neck chain: the model nests neck_yaw under neck_pitch2
// under neck_pitch1, so it is the one node every neck joint has already
// turned. Anchoring higher up the chain would leave a gesture out -- on
// neck_pitch2, a head_shake would not move any of this.
export const HEAD_NODE = 'neck_yaw';

// How sharply two faces have to meet to count as an edge of the shell rather
// than as the curve of it.
const CREASE = Math.PI / 4;
// Below this many corners a ring is a screw boss or a cable notch, not a plate.
const SMALLEST_PLATE = 30;
// A plate turned further than this from the way the robot walks is on the side
// of the head, whatever else it is. Front and back it cannot separate: the
// normal comes from an eigenvector, so its sign is arbitrary and has to be
// folded forward before the angle means anything. Which end of the head the
// plate is on is settled by where it sits, not by which way it points.
const WIDEST_TURN = Math.PI / 3;
// How much of the ring each pass of the circle fit keeps, and how many passes
// it takes. The plate's edge shares its plane with a bracket and a boss, and
// dropping the corners that fit worst is what stops those pulling the middle
// of the face off the middle of the plate.
const KEPT_PER_PASS = 0.85;
const FIT_PASSES = 6;

/**
 * Whether a mesh under the head is the stage's own rather than the robot's.
 *
 * The stage hangs its lens and its screen off this same node, so by the time
 * one of them is mounted the other's geometry is already there to be measured.
 * Nothing the stage adds today yields a crease ring, but a capped cylinder
 * would: a flat rim facing forward beats the real plate on flatness, and the
 * robot's face moves onto its own lens. Cheaper to not measure our own
 * furniture than to rely on the shapes of it.
 *
 * @param {import('three').Object3D} mesh
 * @param {import('three').Object3D} head
 * @returns {boolean}
 */
function isDressing(mesh, head) {
  for (let node = mesh; node !== null && node !== head; node = node.parent) {
    if (node.userData.stageDressing === true) {
      return true;
    }
  }
  return false;
}

/**
 * The head's one mesh, with its corners and triangles in the head's own frame.
 *
 * @param {import('three').Object3D} head
 * @param {import('three').Matrix4} intoHead
 * @returns {{corners: import('three').Vector3[], triangles: number[][]}}
 */
function headMesh(head, intoHead) {
  const corners = [];
  const triangles = [];
  head.traverse((child) => {
    if (!child.isMesh || isDressing(child, head)) {
      return;
    }
    const offset = corners.length;
    const toHead = new Matrix4().multiplyMatrices(intoHead, child.matrixWorld);
    const position = child.geometry.attributes.position;
    for (let i = 0; i < position.count; i += 1) {
      corners.push(new Vector3().fromBufferAttribute(position, i).applyMatrix4(toHead));
    }
    const index = child.geometry.index;
    const count = index === null ? position.count : index.count;
    for (let i = 0; i < count; i += 3) {
      triangles.push([0, 1, 2].map((k) => offset + (index === null ? i + k : index.getX(i + k))));
    }
  });
  if (corners.length === 0) {
    throw new Error(`"${HEAD_NODE}" carries no mesh to measure`);
  }
  return { corners, triangles };
}

/**
 * The direction *points* vary least in -- the normal of the plane they lie in,
 * when they lie in one.
 *
 * @param {import('three').Vector3[]} points
 * @param {import('three').Vector3} middle
 * @returns {import('three').Vector3}
 */
function thinnestAxis(points, middle) {
  const spread = [
    [0, 0, 0],
    [0, 0, 0],
    [0, 0, 0],
  ];
  for (const point of points) {
    const offset = [point.x - middle.x, point.y - middle.y, point.z - middle.z];
    for (let row = 0; row < 3; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        spread[row][column] += offset[row] * offset[column];
      }
    }
  }
  // Turned inside out, so the direction of least spread becomes the one of
  // most, which is the one repeated multiplication converges on.
  const total = spread[0][0] + spread[1][1] + spread[2][2];
  const flipped = spread.map((row, i) => row.map((value, j) => (i === j ? total - value : -value)));
  let axis = [0.31, 0.52, 0.79];
  for (let step = 0; step < 400; step += 1) {
    axis = flipped.map((row) => row[0] * axis[0] + row[1] * axis[1] + row[2] * axis[2]);
    const length = Math.hypot(...axis);
    axis = axis.map((value) => value / length);
  }
  return new Vector3(...axis).normalize();
}

/**
 * The circle *points* lie on, by least squares.
 *
 * @param {{across: number, over: number}[]} points
 * @returns {{across: number, over: number, radius: number}}
 */
function fitCircle(points) {
  let sumAcross = 0;
  let sumOver = 0;
  let sumAcrossSquared = 0;
  let sumOverSquared = 0;
  let sumProduct = 0;
  let sumAcrossRadial = 0;
  let sumOverRadial = 0;
  let sumRadial = 0;
  for (const { across, over } of points) {
    const radial = across * across + over * over;
    sumAcross += across;
    sumOver += over;
    sumAcrossSquared += across * across;
    sumOverSquared += over * over;
    sumProduct += across * over;
    sumAcrossRadial += across * radial;
    sumOverRadial += over * radial;
    sumRadial += radial;
  }
  const rows = [
    [2 * sumAcrossSquared, 2 * sumProduct, sumAcross, sumAcrossRadial],
    [2 * sumProduct, 2 * sumOverSquared, sumOver, sumOverRadial],
    [2 * sumAcross, 2 * sumOver, points.length, sumRadial],
  ];
  for (let step = 0; step < 3; step += 1) {
    let pivot = step;
    for (let row = step + 1; row < 3; row += 1) {
      if (Math.abs(rows[row][step]) > Math.abs(rows[pivot][step])) {
        pivot = row;
      }
    }
    [rows[step], rows[pivot]] = [rows[pivot], rows[step]];
    for (let row = step + 1; row < 3; row += 1) {
      const scale = rows[row][step] / rows[step][step];
      for (let column = step; column < 4; column += 1) {
        rows[row][column] -= scale * rows[step][column];
      }
    }
  }
  const solved = [0, 0, 0];
  for (let step = 2; step >= 0; step -= 1) {
    let value = rows[step][3];
    for (let column = step + 1; column < 3; column += 1) {
      value -= rows[step][column] * solved[column];
    }
    solved[step] = value / rows[step][step];
  }
  const [across, over, offset] = solved;
  return { across, over, radius: Math.sqrt(offset + across * across + over * over) };
}

/**
 * Every ring of sharp edges on the head, as the corners that make it up.
 *
 * @param {{corners: import('three').Vector3[], triangles: number[][]}} mesh
 * @returns {number[][]}
 */
function creaseRings({ corners, triangles }) {
  const normals = triangles.map(([a, b, c]) =>
    new Vector3()
      .subVectors(corners[b], corners[a])
      .cross(new Vector3().subVectors(corners[c], corners[a]))
      .normalize()
  );
  const edges = new Map();
  triangles.forEach((triangle, face) => {
    for (let k = 0; k < 3; k += 1) {
      const [from, to] = [triangle[k], triangle[(k + 1) % 3]].sort((a, b) => a - b);
      const key = `${from}_${to}`;
      if (!edges.has(key)) {
        edges.set(key, []);
      }
      edges.get(key).push(face);
    }
  });

  const beside = new Map();
  for (const [key, faces] of edges) {
    if (faces.length !== 2 || normals[faces[0]].angleTo(normals[faces[1]]) < CREASE) {
      continue;
    }
    for (const [corner, other] of [key.split('_').map(Number), key.split('_').map(Number).reverse()]) {
      if (!beside.has(corner)) {
        beside.set(corner, []);
      }
      beside.get(corner).push(other);
    }
  }

  const found = new Set();
  const rings = [];
  for (const start of beside.keys()) {
    if (found.has(start)) {
      continue;
    }
    const ring = [];
    const walk = [start];
    while (walk.length > 0) {
      const corner = walk.pop();
      if (found.has(corner)) {
        continue;
      }
      found.add(corner);
      ring.push(corner);
      walk.push(...beside.get(corner).filter((next) => !found.has(next)));
    }
    rings.push(ring);
  }
  return rings;
}

/**
 * The plate the robot wears its face on: where its middle is, which way it
 * looks, and how much of it a round screen can cover.
 *
 * Found as the flat thing it is. The head is a solid shell -- there is no hole
 * in the mesh and no plane at right angles to anything -- so neither the
 * bounding box's middle nor the average of the forward-most corners lands on
 * the plate, and a screen placed by either hangs beside the dished front
 * rather than across it. What the plate does have is an edge: the one ring of
 * creases on the head whose corners are all in one plane, to well under a
 * millimetre. Nothing else on the head comes close, and nothing stands in
 * front of it.
 *
 * @param {{corners: import('three').Vector3[], triangles: number[][]}} mesh
 * @param {import('three').Vector3} forward The way the robot walks, in head frame.
 * @param {import('three').Vector3} up The world's up, in head frame.
 * @returns {{middle: import('three').Vector3, normal: import('three').Vector3, radius: number}}
 */
function facePlate(mesh, forward, up) {
  const plates = [];
  for (const ring of creaseRings(mesh)) {
    if (ring.length < SMALLEST_PLATE) {
      continue;
    }
    const corners = ring.map((corner) => mesh.corners[corner]);
    const middle = corners
      .reduce((total, corner) => total.add(corner.clone()), new Vector3())
      .divideScalar(corners.length);
    const normal = thinnestAxis(corners, middle);
    if (normal.dot(forward) < 0) {
      normal.negate();
    }
    // The neck node sits behind the shell, so a plate the robot could look out
    // of is ahead of the head's own origin. Without this a ring on the back of
    // the skull scores a perfect zero on the turn test below.
    if (middle.dot(forward) <= 0 || normal.angleTo(forward) > WIDEST_TURN) {
      continue;
    }
    const outOfPlane = corners.map((corner) => Math.abs(corner.clone().sub(middle).dot(normal)));
    plates.push({
      middle,
      normal,
      corners,
      flatness: outOfPlane.reduce((total, value) => total + value, 0) / outOfPlane.length,
    });
  }
  if (plates.length === 0) {
    throw new Error('the head has no plate big enough or flat enough to wear a face');
  }

  const plate = plates.reduce((flattest, next) => (next.flatness < flattest.flatness ? next : flattest));
  const across = new Vector3().crossVectors(up, plate.normal).normalize();
  const over = new Vector3().crossVectors(plate.normal, across).normalize();

  let onCircle = plate.corners.map((corner) => {
    const offset = corner.clone().sub(plate.middle);
    return { across: offset.dot(across), over: offset.dot(over) };
  });
  let circle = fitCircle(onCircle);
  for (let pass = 0; pass < FIT_PASSES; pass += 1) {
    const missBy = ({ across: x, over: y }) =>
      Math.abs(Math.hypot(x - circle.across, y - circle.over) - circle.radius);
    onCircle = [...onCircle]
      .sort((a, b) => missBy(a) - missBy(b))
      .slice(0, Math.max(12, Math.round(onCircle.length * KEPT_PER_PASS)));
    circle = fitCircle(onCircle);
  }

  return {
    middle: plate.middle
      .clone()
      .addScaledVector(across, circle.across)
      .addScaledVector(over, circle.over),
    normal: plate.normal,
    radius: circle.radius,
  };
}

/**
 * The head node, the robot's own axes inside it, and the face it wears them on.
 *
 * `forward` is the direction `forward()` carries the body and `up` is the
 * world's, both in the head's local frame -- so anything parented to the head
 * can be placed by them and will then travel with every neck joint. `facing`
 * is the way the face looks, which is `forward` only while the neck is at
 * neutral -- the face is measured, not assumed, so a turned neck reports the
 * way it is turned. `eye` is the middle of the face and `across` is how wide
 * it is, which is what everything the stage lays on the face is sized and
 * placed by.
 *
 * @param {import('three').Object3D} model The model root, at rest.
 * @param {Map<string, {node: import('three').Object3D}>} joints As `readJoints` returns them.
 * @returns {{head: import('three').Object3D, forward: import('three').Vector3, up: import('three').Vector3, eye: import('three').Vector3, facing: import('three').Vector3, across: number}}
 */
export function headAnchor(model, joints) {
  const joint = joints.get(HEAD_NODE);
  if (joint === undefined) {
    throw new Error(`the model has no joint named "${HEAD_NODE}"`);
  }
  const head = joint.node;
  model.updateMatrixWorld(true);

  const intoHead = new Matrix4().copy(head.matrixWorld).invert();
  const forward = new Vector3(1, 0, 0).transformDirection(intoHead).normalize();
  const up = new Vector3(0, 1, 0).transformDirection(intoHead).normalize();
  const plate = facePlate(headMesh(head, intoHead), forward, up);

  return {
    head,
    forward,
    up,
    eye: plate.middle,
    facing: plate.normal,
    across: plate.radius * 2,
  };
}
