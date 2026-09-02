// The lit stage the robot stands on: a three.js scene over one canvas.
//
// It renders and it turns joints; it does not decide what the joints should be.
// That keeps the door open for the page to hand it frames the SDK computed,
// rather than this file growing a second opinion about how the robot moves.
import {
  Box3,
  Color,
  DirectionalLight,
  Fog,
  Group,
  GridHelper,
  Mesh,
  NeutralToneMapping,
  PCFSoftShadowMap,
  PMREMGenerator,
  PerspectiveCamera,
  PlaneGeometry,
  Scene,
  ShadowMaterial,
  Vector3,
  WebGLRenderer,
} from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { advanceTravel, standOnFeet, stepBetween } from './robot-ground.js';
import { mountFace } from './robot-face.js';
import { poseJoints, readJoints } from './robot-pose.js';
import { FRAME_HEIGHT, FRAME_WIDTH, insetViewport, mountView } from './robot-view.js';

const LEG_COUNT = 6;
const UP = new Vector3(0, 1, 0);

// One orbit every 45 seconds: slow enough to read as an invitation to drag
// rather than as an animation playing.
const ORBIT_SECONDS = 45;
const AUTO_ROTATE_SPEED = (2.0 * 30) / ORBIT_SECONDS;
// Where the camera sits, as a fraction of the model's own size, so a re-export
// that changes the robot's dimensions cannot quietly crop it.
const DISTANCE_IN_MODEL_SIZES = 1.5;
const CAMERA_HEIGHT_IN_MODEL_SIZES = 0.55;
// How far the reader may orbit in or out, in model sizes: close enough to
// still resolve the robot, and short of where the fog starts so it never
// dissolves.
const ORBIT_MIN_DISTANCE_IN_MODEL_SIZES = 0.75;
const ORBIT_MAX_DISTANCE_IN_MODEL_SIZES = 4;

// The ground the robot walks over, also in model sizes. A cell is about a
// fifth of the robot, so a stride crosses one and travel reads as travel; the
// sheet is wide enough that the fog takes it before its edge can.
const GRID_CELL_IN_MODEL_SIZES = 0.2;
const GRID_CELLS = 120;
// Held past where a reader can orbit to, so the robot is never in the haze.
const FOG_NEAR_IN_MODEL_SIZES = 5;
const FOG_FAR_IN_MODEL_SIZES = 11;

/**
 * A colour the stage's own stylesheet decides, so the two cannot drift apart.
 *
 * @param {HTMLElement} element
 * @param {string} property
 * @param {string} fallback
 * @returns {import('three').Color}
 */
function themeColour(element, property, fallback) {
  const declared = getComputedStyle(element).getPropertyValue(property).trim();
  return new Color(declared || fallback);
}

/**
 * The lowest point of a link's own surface, in world space.
 *
 * @param {import('three').Object3D} node
 * @param {string} name
 * @returns {import('three').Vector3}
 */
function lowestVertex(node, name) {
  let lowest = null;
  const vertex = new Vector3();
  node.traverse((child) => {
    if (!child.isMesh) {
      return;
    }
    const position = child.geometry.attributes.position;
    for (let i = 0; i < position.count; i += 1) {
      vertex.fromBufferAttribute(position, i).applyMatrix4(child.matrixWorld);
      if (lowest === null || vertex.y < lowest.y) {
        lowest = vertex.clone();
      }
    }
  });
  if (lowest === null) {
    throw new Error(`"${name}" carries no mesh to stand on`);
  }
  return lowest;
}

/**
 * Build the scene, load the model, and start drawing.
 *
 * @param {HTMLCanvasElement} canvas
 * @param {string} modelUrl
 * @param {HTMLElement} [viewFrame] Where to draw what the robot sees. The
 *   element's own box is the viewport, so the frame the page draws around the
 *   inset and the picture inside it cannot come apart. Omitted, there is no inset.
 * @param {{mic: HTMLElement}} [signals] Where the peripheral with nothing of
 *   its own to draw reports itself: the mic, while a capture is running.
 * @returns {Promise<{pose: (angles: Record<string, number>) => void, read: () => Uint8Array, dispose: () => void}>}
 */
export async function mountStage(canvas, modelUrl, viewFrame, signals) {
  const renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFSoftShadowMap;
  // The Khronos neutral mapper rather than a filmic one: the finishes are the
  // product's palette, and a filmic curve lifts and desaturates them until the
  // walnut head reads as something the robot is not made of.
  renderer.toneMapping = NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;

  const scene = new Scene();
  // A neutral room, the same lighting model-viewer's "neutral" preset gave the
  // robot: it is a matte printed frame, so it needs bounced light to read at all.
  const pmrem = new PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const camera = new PerspectiveCamera(35, 1, 0.01, 100);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.autoRotate = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  controls.autoRotateSpeed = AUTO_ROTATE_SPEED;

  const { scene: model } = await new GLTFLoader().loadAsync(modelUrl);
  model.traverse((node) => {
    if (node.isMesh) {
      node.castShadow = true;
    }
  });
  scene.add(model);

  const joints = readJoints(model);
  model.updateMatrixWorld(true);

  // Where each leg touches the ground: the lowest vertex of the link, not the
  // middle of its bounding box -- the link is slanted, so the box's centre is
  // nowhere near the surface that meets the floor. Measured once at rest, on
  // the six nodes export_web_model.py names leg_N_pitch2, and kept twice: in
  // the joint's own frame so the point travels as poseJoints turns it, and in
  // the model's frame as the height every later stance is measured against.
  const feet = [];
  const restFeet = [];
  for (let leg = 1; leg <= LEG_COUNT; leg += 1) {
    const name = `leg_${leg}_pitch2`;
    const joint = joints.get(name);
    if (joint === undefined) {
      throw new Error(`the model has no joint named "${name}"`);
    }
    const tipWorld = lowestVertex(joint.node, name);
    restFeet.push(model.worldToLocal(tipWorld.clone()));
    feet.push({ node: joint.node, point: joint.node.worldToLocal(tipWorld) });
  }

  // These describe the rest pose and must stay fixed while the body moves --
  // recomputing them from the posed model would chase the very thing that is
  // supposed to be standing still.
  const bounds = new Box3().setFromObject(model);
  const size = bounds.getSize(new Vector3());
  const centre = bounds.getCenter(new Vector3());
  const reach = Math.max(size.x, size.y, size.z);

  // The robot casts onto a plane at its feet rather than onto the page, so the
  // stage reads as a surface the robot stands on instead of a cut-out. The
  // grid over it is what makes walking legible: a robot travelling across a
  // blank sheet is indistinguishable from one marking time.
  const cell = reach * GRID_CELL_IN_MODEL_SIZES;
  const ground = new Group();
  ground.position.y = bounds.min.y;
  scene.add(ground);

  const shadowCatcher = new Mesh(
    new PlaneGeometry(reach * 6, reach * 6),
    new ShadowMaterial({ opacity: 0.22 })
  );
  shadowCatcher.rotation.x = -Math.PI / 2;
  shadowCatcher.receiveShadow = true;
  // Fog would tint the shadow rather than fade it, since a ShadowMaterial
  // carries the shadow in its alpha and not in its colour.
  shadowCatcher.material.fog = false;
  ground.add(shadowCatcher);

  const rule = themeColour(canvas, '--stage-rule', '#ddd3bf');
  const grid = new GridHelper(cell * GRID_CELLS, GRID_CELLS, rule, rule);
  ground.add(grid);

  // The sheet has an edge, and the robot walks far enough to reach for it.
  // Fogging to the plate the canvas is transparent over hides it exactly: past
  // the far distance the lines have become the page they are drawn on.
  const backdrop = themeColour(canvas, '--stage-ground', '#f3efe7');
  scene.fog = new Fog(
    backdrop,
    reach * FOG_NEAR_IN_MODEL_SIZES,
    reach * FOG_FAR_IN_MODEL_SIZES
  );

  const sun = new DirectionalLight(0xffffff, 2.2);
  const sunOffset = new Vector3(reach, reach * 2, reach * 1.5);
  sun.position.copy(sunOffset);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.camera.near = 0.01;
  sun.shadow.camera.far = reach * 8;
  const extent = reach * 1.2;
  Object.assign(sun.shadow.camera, { left: -extent, right: extent, top: extent, bottom: -extent });
  sun.shadow.camera.updateProjectionMatrix();
  scene.add(sun);
  scene.add(sun.target);
  sun.target.position.copy(centre);

  controls.target.copy(centre);
  controls.minDistance = reach * ORBIT_MIN_DISTANCE_IN_MODEL_SIZES;
  controls.maxDistance = reach * ORBIT_MAX_DISTANCE_IN_MODEL_SIZES;
  camera.position.set(
    centre.x + reach * DISTANCE_IN_MODEL_SIZES,
    centre.y + reach * CAMERA_HEIGHT_IN_MODEL_SIZES,
    centre.z + reach * DISTANCE_IN_MODEL_SIZES
  );
  controls.update();

  // Everything that has to stay with the robot once it stops standing still.
  // The camera keeps whatever angle and distance the reader dragged it to,
  // because it is moved by the same step as its target rather than re-aimed.
  const aim = new Vector3();
  function follow() {
    aim.copy(centre).applyQuaternion(model.quaternion).add(model.position);
    camera.position.add(aim.clone().sub(controls.target));
    controls.target.copy(aim);
    sun.position.copy(aim).add(sunOffset);
    sun.target.position.copy(aim);
    // A grid is unchanged by a move of a whole number of cells, so keeping it
    // under the robot to the nearest cell makes it a ground without an end.
    ground.position.x = Math.round(aim.x / cell) * cell;
    ground.position.z = Math.round(aim.z / cell) * cell;
  }
  follow();

  // What the robot sees. It is a child of the head, so it needs nothing from
  // the frame loop beyond being drawn.
  const view = mountView(model, joints, backdrop);
  const face = mountFace(model, joints);
  let listening = 0;

  // Where the inset goes, measured off the element the page draws around it
  // rather than agreed with it. Read on resize, not per frame: it is a layout
  // read, and it only moves when the canvas does.
  let inset = null;
  function measureInset() {
    inset =
      viewFrame === undefined || viewFrame === null
        ? null
        : insetViewport(canvas.getBoundingClientRect(), viewFrame.getBoundingClientRect());
  }

  function resize() {
    const { clientWidth, clientHeight } = canvas;
    if (clientWidth === 0 || clientHeight === 0) {
      return;
    }
    renderer.setSize(clientWidth, clientHeight, false);
    camera.aspect = clientWidth / clientHeight;
    camera.updateProjectionMatrix();
    measureInset();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  if (viewFrame !== undefined && viewFrame !== null) {
    // The page only reveals the inset's frame once the stage has started, so
    // its box arrives after this measurement would otherwise have been taken.
    observer.observe(viewFrame);
  }
  resize();

  // The inset is a second pass over the same canvas, so the renderer must stop
  // clearing on its own: the pass would wipe the picture the first one drew.
  renderer.autoClear = false;

  let frame = 0;
  function draw(now = performance.now()) {
    frame = requestAnimationFrame(draw);
    controls.update();
    // The face is an animation the firmware runs, so it is carried by the
    // clock rather than repainted once when the command lands.
    face.update(now);
    renderer.clear();
    renderer.render(scene, camera);
    if (inset !== null) {
      // Scissored, so the clear that gives this pass its own depth buffer
      // reaches only the corner it is drawn in.
      renderer.setScissorTest(true);
      renderer.setViewport(inset.x, inset.y, inset.width, inset.height);
      renderer.setScissor(inset.x, inset.y, inset.width, inset.height);
      renderer.clear();
      renderer.render(scene, view.camera);
      renderer.setScissorTest(false);
      renderer.setViewport(0, 0, canvas.clientWidth, canvas.clientHeight);
    }
  }
  draw();

  let travel = { turn: 0, position: new Vector3() };
  let lastStance = null;

  function pose(angles) {
    poseJoints(joints, angles);
    model.updateMatrixWorld(true);

    // Measured in the model's own space, not world space: that divides out
    // the root transform this function is about to overwrite, so the fit
    // never feeds back on itself.
    const stance = feet.map(({ node, point }) => model.worldToLocal(node.localToWorld(point.clone())));

    if (lastStance !== null) {
      travel = advanceTravel(travel, stepBetween(stance, lastStance, restFeet));
    }
    lastStance = stance;

    // Where the robot has walked to has no anchor to be recovered from, so it
    // accumulates; how it is standing does, so it is measured afresh. Applied
    // in that order, because the lean is the robot's own and the heading is
    // the world's.
    const stand = standOnFeet(stance, restFeet);
    model.quaternion.setFromAxisAngle(UP, travel.turn).multiply(stand.quaternion);
    model.position.copy(travel.position).add(stand.position);
    model.updateMatrixWorld(true);
    follow();
  }

  return {
    pose,
    /**
     * One frame from the robot's own camera.
     *
     * Rendered on demand rather than kept: the inset on screen is drawn
     * straight to the canvas, so nothing is read back off the GPU until
     * someone asks for the pixels themselves. The size travels with the
     * pixels so that whatever reads them does not need its own copy of it.
     *
     * @returns {{width: number, height: number, data: Uint8Array}} Top-down BGR.
     */
    read: () => ({ width: FRAME_WIDTH, height: FRAME_HEIGHT, data: view.read(renderer, scene) }),
    /**
     * Show an expression on the robot's own screen.
     *
     * @param {string} name
     */
    face: (name) => face.show(name),
    /**
     * Run the capture meter for *seconds*, so a recording is something a
     * reader watches happen rather than a number that comes back.
     *
     * @param {number} seconds
     */
    listen: (seconds) => {
      const meter = signals?.mic;
      if (meter === undefined) {
        return;
      }
      meter.style.setProperty('--capture-seconds', `${seconds}s`);
      // Restarting an animation means taking the class off and letting the
      // browser notice before putting it back.
      meter.classList.remove('is-live');
      void meter.offsetWidth;
      meter.classList.add('is-live');
      clearTimeout(listening);
      listening = setTimeout(() => meter.classList.remove('is-live'), seconds * 1000);
    },
    dispose: () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      view.dispose();
      face.dispose();
      clearTimeout(listening);
      controls.dispose();
      pmrem.dispose();
      renderer.dispose();
    },
  };
}
