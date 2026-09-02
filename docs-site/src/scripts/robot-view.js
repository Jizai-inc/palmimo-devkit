// What the robot sees, from the robot's own head, and the lens it sees it
// through.
//
// There is no camera in the export -- it carries the mechanical parts and
// nothing else, and its shell is closed, so there is no aperture to find and
// no lens to find. Both are the stage's own, and both are placed off the face
// the asset does carry: on its axis, in the window cut through the bracket
// below the plate, which is the widest opening the head has. The face itself
// is measured rather than written down, so a re-export that reframes the head
// carries them with it.
//
// They are children of the head link, so every neck joint the SDK turns has
// already carried them before they are drawn; nothing here holds a second
// opinion about where the head is pointing.
import {
  CircleGeometry,
  Color,
  CylinderGeometry,
  DoubleSide,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  PerspectiveCamera,
  RingGeometry,
  Vector3,
  WebGLRenderTarget,
} from 'three';
import { headAnchor } from './robot-head.js';

// Wide enough that turning the neck sweeps visibly rather than creeping.
const FIELD_OF_VIEW = 62;
// How far below level the lens is set in its mount, in radians (about 18
// degrees). A mount angle, not a pose: the neck moves it, the neck does not
// set it. The lens rides 293 mm above the ground the stage stands the robot
// on; dead level its axis never meets that ground at all, so the frame is
// horizon and whatever the fog has not taken. Angled down, the axis meets the
// ground 904 mm out and the frame holds what the robot is about to walk over,
// which is what a camera on a desk robot is for.
export const DECLINATION = 0.314;
// Clipping, in metres. Near is inside the head's own shell, so the plate the
// lens looks out of never fills the frame; far matches the stage's own camera.
const NEAR = 0.01;
const FAR = 100;
// How far the camera stands off the face plate, as a fraction of the plate's
// width. The bracket's top rail juts 9.0 mm in front of the plate directly
// above the window, so a camera set any closer than that carries a rail across
// the top of every frame it returns.
export const LENS_STANDOFF = 0.12;
// Where the lens sits on the face and how wide it is, as fractions of the
// face's width. The head is one closed shell, but three openings are cut
// through it below the face plate, and the camera looks out of the widest: the
// 149 mm2 window through the bracket under the plate. Both numbers are that
// window's -- the middle of the largest circle that fits inside it, and that
// circle's width -- so the lens fills the hole instead of floating in it.
// `robot-view.test.mjs` casts rays at the shipped asset and holds them there.
export const LENS_BELOW = 0.6526;
export const LENS_ACROSS = 0.1263;
// The bore inside the bezel, and how deep it runs, same units. The bracket is
// 1.9 mm thick at the window, so a lens whose glass sat in its mouth would be
// a disc painted on; the module behind it holds the glass about that much
// again further back.
const BORE_ACROSS = 0.091;
const BORE_DEEP = 0.045;
// How wide the black backing behind the bore is, same units. Wider than the
// window and set behind the 1.9 mm wall it is cut through, so it is hidden
// from the front and the head has no hole to see daylight through from the
// back. Without it the window is a window: orbiting round the robot looks
// straight through its head.
const CAP_ACROSS = 0.21;
// How far the bezel stands off the face plane, same units. The bracket around
// the window sits 0.6 mm behind that plane, so this leaves the lens under a
// millimetre proud of it -- fitted into the hole, not stuck over it.
const LENS_PROUD = 0.002;

// The frame the robot's camera hands back. The aspect is the real camera's
// (`HeadCameraConfig` downscales to 1296x972); the size is the inset's, so
// what a reader sees and what `read()` returns are one picture and not two.
export const FRAME_WIDTH = 320;
export const FRAME_HEIGHT = 240;

/**
 * One readback's pixels, as the frame an OpenCV caller would recognise.
 *
 * WebGL hands back RGBA with the bottom row first; a camera's frame is BGR
 * with the top row first. Both differences are undone here rather than left
 * for the reader, so `read()` returns what the real camera returns.
 *
 * @param {Uint8Array} rgba Bottom-up RGBA, four bytes per pixel.
 * @param {number} width
 * @param {number} height
 * @returns {Uint8Array} Top-down BGR, three bytes per pixel.
 */
export function bgrFromReadback(rgba, width, height) {
  if (rgba.length < width * height * 4) {
    throw new Error(`a ${width}x${height} readback needs ${width * height * 4} bytes, got ${rgba.length}`);
  }
  const bgr = new Uint8Array(width * height * 3);
  for (let row = 0; row < height; row += 1) {
    const from = (height - 1 - row) * width * 4;
    const to = row * width * 3;
    for (let column = 0; column < width; column += 1) {
      bgr[to + column * 3] = rgba[from + column * 4 + 2];
      bgr[to + column * 3 + 1] = rgba[from + column * 4 + 1];
      bgr[to + column * 3 + 2] = rgba[from + column * 4];
    }
  }
  return bgr;
}

/**
 * The inset's place on the canvas, as a viewport.
 *
 * A page counts down from the top and a viewport up from the bottom, so the
 * frame's box has to be turned over to become one. Both boxes are in the same
 * coordinates -- whatever `getBoundingClientRect` returned them in.
 *
 * @param {{left: number, bottom: number}} canvasBox The canvas's own box.
 * @param {{left: number, bottom: number, width: number, height: number}} frameBox
 * @returns {{x: number, y: number, width: number, height: number} | null} Null
 *   when the frame has no box to draw in -- it has not been revealed yet.
 */
export function insetViewport(canvasBox, frameBox) {
  if (frameBox.width === 0 || frameBox.height === 0) {
    return null;
  }
  return {
    x: frameBox.left - canvasBox.left,
    y: canvasBox.bottom - frameBox.bottom,
    width: frameBox.width,
    height: frameBox.height,
  };
}

/**
 * A lens for the camera to look out of.
 *
 * The camera works without one. The reader does not: they are being told the
 * robot has a camera and shown where it looks from, and the window it looks
 * out of is empty in the export.
 *
 * Built as a bezel with a bore behind it rather than as a disc, because the
 * hole it fills has a wall with thickness and the head is seen from every
 * angle the orbit reaches -- a disc reads as a sticker the moment the robot
 * turns.
 *
 * @param {number} across The face's width, which the lens is sized against.
 * @returns {import('three').Mesh} Facing +z, to be turned to the face.
 */
function dummyLens(across) {
  const bezel = new Mesh(
    new RingGeometry((across * BORE_ACROSS) / 2, (across * LENS_ACROSS) / 2, 32),
    new MeshBasicMaterial({ color: 0x2a2320, toneMapped: false })
  );
  const bore = new Mesh(
    new CylinderGeometry(
      (across * BORE_ACROSS) / 2,
      (across * BORE_ACROSS) / 2,
      across * BORE_DEEP,
      32,
      1,
      true
    ).rotateX(Math.PI / 2),
    // Seen from the front, the far wall of the bore is a back face.
    new MeshBasicMaterial({ color: 0x15100d, toneMapped: false, side: DoubleSide })
  );
  bore.position.z = (-across * BORE_DEEP) / 2;
  const cap = new Mesh(
    new CircleGeometry((across * CAP_ACROSS) / 2, 32),
    new MeshBasicMaterial({ color: 0x080706, toneMapped: false, side: DoubleSide })
  );
  cap.position.z = -across * BORE_DEEP;
  const glass = new Mesh(
    new CircleGeometry((across * BORE_ACROSS) / 2, 32),
    new MeshBasicMaterial({ color: 0x0a1119, toneMapped: false })
  );
  glass.position.z = -across * (BORE_DEEP - 0.001);
  const glint = new Mesh(
    new CircleGeometry((across * BORE_ACROSS) / 6, 16),
    new MeshBasicMaterial({ color: 0x8ea6bd, toneMapped: false, transparent: true, opacity: 0.55 })
  );
  glint.position.set(-across * BORE_ACROSS * 0.2, across * BORE_ACROSS * 0.2, across * 0.0004);
  glass.add(glint);
  bezel.add(bore, cap, glass);
  return bezel;
}

/**
 * Put a camera on the robot's head and hand back the ways to use it.
 *
 * @param {import('three').Object3D} model The posed model root.
 * @param {Map<string, {node: import('three').Object3D}>} joints As `readJoints` returns them.
 * @param {import('three').Color} backdrop What the stage stands against. On
 *   screen the canvas is transparent and the page supplies this; a frame read
 *   off the GPU has no page behind it, so it has to be painted in or the sky
 *   comes back as a black void the viewer never saw.
 * @returns {{camera: import('three').PerspectiveCamera, read: (renderer: any, scene: any) => Uint8Array, dispose: () => void}}
 */
export function mountView(model, joints, backdrop) {
  const { head, forward, up, eye, facing, across } = headAnchor(model, joints);
  // Down the face, not down the world: the face is tilted, so a lens dropped
  // by the world's own down would walk out of the band it belongs in.
  const down = up.clone().addScaledVector(facing, -up.dot(facing)).normalize().negate();
  const at = eye.clone().addScaledVector(down, across * LENS_BELOW);
  const toFace = new Matrix4().lookAt(new Vector3(), facing.clone().negate(), up);

  const lens = dummyLens(across);
  lens.name = 'camera_lens';
  // Furniture the stage hung on the head, not part of the head to be measured.
  lens.userData.stageDressing = true;
  lens.position.copy(at).addScaledVector(facing, across * LENS_PROUD);
  lens.quaternion.setFromRotationMatrix(toFace);
  head.add(lens);

  const camera = new PerspectiveCamera(FIELD_OF_VIEW, FRAME_WIDTH / FRAME_HEIGHT, NEAR, FAR);
  // Out past the lens it looks through, and past the screen laid on the same
  // face: a camera left behind either sees the back of it and nothing else.
  // Today the near plane hides that anyway, which is exactly why it is worth
  // not depending on.
  camera.position.copy(at).addScaledVector(facing, across * LENS_STANDOFF);
  const from = camera.position.clone();
  camera.quaternion.setFromRotationMatrix(new Matrix4().lookAt(from, from.clone().add(forward), up));
  camera.rotateX(-DECLINATION);
  head.add(camera);

  // Only built if a reader ever asks for a frame: the inset on screen is drawn
  // straight to the canvas, so nothing needs an off-screen copy until the
  // pixels have to come back to Python.
  let target = null;

  return {
    camera,
    /**
     * One frame, read back off the GPU.
     *
     * @param {import('three').WebGLRenderer} renderer
     * @param {import('three').Scene} scene
     * @returns {Uint8Array} Top-down BGR.
     */
    read: (renderer, scene) => {
      target ??= new WebGLRenderTarget(FRAME_WIDTH, FRAME_HEIGHT);
      const previousTarget = renderer.getRenderTarget();
      const previousColour = renderer.getClearColor(new Color());
      const previousAlpha = renderer.getClearAlpha();
      renderer.setRenderTarget(target);
      // Painted, not merely emptied. The stage draws two passes over one
      // canvas, so the renderer's own clearing is off and this target would
      // otherwise accumulate every frame ever read -- and it has no page
      // behind it to be transparent over.
      renderer.setClearColor(backdrop, 1);
      renderer.clear();
      renderer.render(scene, camera);
      const rgba = new Uint8Array(FRAME_WIDTH * FRAME_HEIGHT * 4);
      renderer.readRenderTargetPixels(target, 0, 0, FRAME_WIDTH, FRAME_HEIGHT, rgba);
      renderer.setRenderTarget(previousTarget);
      renderer.setClearColor(previousColour, previousAlpha);
      return bgrFromReadback(rgba, FRAME_WIDTH, FRAME_HEIGHT);
    },
    dispose: () => {
      camera.removeFromParent();
      lens.removeFromParent();
      lens.traverse((part) => {
        part.geometry?.dispose();
        part.material?.dispose();
      });
      target?.dispose();
      target = null;
    },
  };
}
