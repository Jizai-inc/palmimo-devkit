// The screen the robot looks back at you with.
//
// The export is the mechanical parts and nothing else -- `neck_yaw` is the
// shade the head is, closed and solid, and the round display the real robot
// wears in it is not in the mesh. So this is the stage's own addition, laid
// on the flat face of that cone, which is where the real one shows through.
//
// What it draws is the robot's own face, not an impression of one. Palmimo
// does not wear eyes and a mouth: its display is a lit body that changes
// colour, size and shape, drawn from a state vector rather than from any
// picture. The states below, their timings and their easing are the firmware's
// -- the same numbers its display runs on -- and the drawing follows the same
// steps: a core that fades into a glow, and a circular lid that comes down
// over it. Two things are the stage's own reading rather than the firmware's:
// the heart is an outline solved from the same implicit curve but lit with a
// blur instead of the firmware's distance field, and the lid's soft edge is a
// gradient rather than a per-pixel feather.
import { CanvasTexture, CircleGeometry, Matrix4, Mesh, MeshBasicMaterial, SRGBColorSpace, Vector3 } from 'three';
import { headAnchor } from './robot-head.js';

// How much of the head's face the screen covers, and how far clear of it the
// screen sits, both as fractions of that face's width.
//
// The share is the robot's own: it wears a 2.8 inch round panel, which on a
// round display is the width of the lit circle -- 71.1 mm -- and the face
// measures 95.0 mm across.
//
// Clear of the face, not sunk into it: the real head is a shade with the
// display down inside, but the export is a solid cone with no cavity, so
// anything set back from its front face is inside the mesh and invisible.
export const SCREEN_ACROSS = 0.75;
export const SCREEN_STANDOFF = 0.01;

// The panel is 480 by 480, so the texture is too, and every length below is in
// the firmware's own pixels: the radius a state's offsets are measured in, the
// lit core, the width it fades over, and the lid's radius and edge.
const PANEL = 480;
const RADIUS = 240;
const CORE = 150;
const GLOW_WIDTH = 88;
const LID_RADIUS = 339.41;
const LID_EDGE = 55;
const HEART_SIZE = 140;
// The heart sits high in its own outline, so the firmware drops it to put it
// in the middle of the panel by eye.
const HEART_DROP = 0.12;

// What the display rests at, and what every state is a departure from.
const REST = { hue: 42, sat: 1, bri: 1, cx: 0, cy: 0, sx: 1, sy: 1, glow: 1, clid: 0 };

// In the firmware's order, because the timelines below carry its numbers.
const EASE = [
  (f) => f,
  (f) => 0.5 - 0.5 * Math.cos(Math.PI * f),
  (f) => 1 - (1 - f) * (1 - f),
  (f) => f * f,
  (f) => {
    const back = 1.70158;
    const past = f - 1;
    return 1 + (back + 1) * past * past * past + back * past * past;
  },
];

// The firmware's expressions: `[milliseconds, easing, what changes]`, each
// keyframe carrying forward whatever the one before it left.
const EXPRESSIONS = {
  IDLE: { keys: [[0, 0, {}]] },
  HAPPY: {
    keys: [
      [0, 0, {}],
      [200, 2, { bri: 1.08, cy: -0.13 }],
      [400, 1, { bri: 1.03, cy: 0.03 }],
      [560, 2, { bri: 1.08, cy: -0.13 }],
      [720, 1, { bri: 1.03, cy: 0.03 }],
      [880, 2, { bri: 1.08, cy: -0.13 }],
      [1040, 1, { bri: 1.03, cy: 0.03 }],
      [1450, 0, {}],
    ],
  },
  EXCITED: {
    keys: [
      [0, 0, {}],
      [120, 2, { bri: 1.07, sx: 1.05, sy: 1.05, glow: 1.08 }],
      [230, 1, { bri: 1.0, sx: 0.99, sy: 0.99, glow: 1.0 }],
      [350, 2, { bri: 1.12, sx: 1.08, sy: 1.08, glow: 1.15 }],
      [470, 1, { bri: 1.0, sx: 1.0, sy: 1.0, glow: 1.0 }],
      [590, 2, { bri: 1.06, sx: 1.05, sy: 1.05, glow: 1.08 }],
      [720, 1, { bri: 1.0, sx: 1.0, sy: 1.0, glow: 1.0 }],
      [1050, 1, {}],
    ],
  },
  SURPRISE: {
    keys: [
      [0, 0, {}],
      [140, 2, { bri: 1.16, sx: 1.16, sy: 1.16, glow: 0.68 }],
      [640, 0, {}],
      [1050, 1, {}],
    ],
  },
  THINKING: {
    keys: [
      [0, 0, {}],
      [400, 2, { cx: 0.08, cy: -0.13, sx: 0.85, sy: 0.85 }],
      [650, 1, { cx: 0.06, cy: -0.11 }],
      [1300, 0, {}],
      [1700, 1, { cx: -0.05, cy: -0.12 }],
      [2300, 0, {}],
      [2800, 1, { cx: 0.06, cy: -0.11 }],
    ],
  },
  ANGRY: {
    keys: [
      [0, 0, {}],
      [360, 2, { hue: 4, sat: 0.9, sx: 0.92, sy: 0.92, glow: 0.7, clid: 0.28 }],
      [900, 0, {}],
      [1500, 1, { bri: 1.09, sx: 0.94, sy: 0.895, glow: 0.78, clid: 0.31 }],
      [2400, 1, { bri: 1.0, sx: 0.92, sy: 0.92, glow: 0.7, clid: 0.28 }],
    ],
  },
  SAD: {
    keys: [
      [0, 0, {}],
      [550, 1, { sat: 0.1, bri: 0.82, cy: 0.06, sx: 0.93, sy: 0.91, glow: 0.9 }],
      [1100, 1, { hue: 218, sat: 0.55, bri: 0.72, cy: 0.14, sx: 0.8, sy: 0.8, glow: 0.78 }],
      [2200, 1, { sat: 0.5, bri: 0.68, cy: 0.165, sx: 0.79, sy: 0.79, glow: 0.74 }],
      [3400, 1, { sat: 0.55, bri: 0.72, cy: 0.14, sx: 0.8, sy: 0.8, glow: 0.78 }],
    ],
  },
  SLEEPY: {
    keys: [
      [0, 0, {}],
      [1000, 1, { hue: 58, sat: 0.28, bri: 0.66, sx: 0.97, sy: 0.96, clid: 0.4 }],
      [1700, 1, { hue: 72, sat: 0.18, bri: 0.54, sx: 0.95, sy: 0.94, clid: 0.72 }],
      [2600, 1, { hue: 73, sat: 0.17, bri: 0.5, sx: 0.945, sy: 0.935, clid: 0.75 }],
      [3600, 1, { hue: 72, sat: 0.18, bri: 0.54, sx: 0.95, sy: 0.94, clid: 0.72 }],
    ],
  },
  SLEEPING: {
    keys: [
      [0, 0, { hue: 70, sat: 0.16, bri: 0.58, sx: 0.93, sy: 0.93, glow: 0.85, clid: 0.76 }],
      [1400, 1, { bri: 0.66, sx: 0.95, sy: 0.94, clid: 0.72 }],
      [2800, 1, { bri: 0.54, sx: 0.92, sy: 0.91, clid: 0.79 }],
      [4200, 1, { bri: 0.58, sx: 0.93, sy: 0.93, clid: 0.76 }],
    ],
  },
  LOVE: {
    heart: true,
    keys: [
      [0, 0, { hue: 345, sat: 0.85, sx: 0.01, sy: 0.01 }],
      [300, 4, { bri: 1.05, sx: 1, sy: 1 }],
      [375, 2, { bri: 1.14, sx: 1.14, sy: 1.14 }],
      [450, 1, { bri: 1.0, sx: 1.02, sy: 1.02 }],
      [525, 2, { bri: 1.07, sx: 1.08, sy: 1.08 }],
      [615, 1, { bri: 1.0, sx: 1.0, sy: 1.0 }],
      [900, 0, {}],
      [975, 2, { bri: 1.14, sx: 1.14, sy: 1.14 }],
      [1050, 1, { bri: 1.0, sx: 1.02, sy: 1.02 }],
      [1125, 2, { bri: 1.07, sx: 1.08, sy: 1.08 }],
      [1215, 1, { bri: 1.0, sx: 1.0, sy: 1.0 }],
      [1500, 1, {}],
    ],
  },
};

// The names the SDK offers, against what the firmware resolves each of them
// to. Its own table, so the page answers `set_expression("smile")` with the
// face the robot would show rather than one invented for the web.
const RESOLVES_TO = {
  IDLE: 'IDLE',
  HAPPY: 'HAPPY',
  SMILE: 'HAPPY',
  LAUGH: 'EXCITED',
  IDEA: 'EXCITED',
  SURPRISED: 'SURPRISE',
  THINKING: 'THINKING',
  ANGRY: 'ANGRY',
  SAD: 'SAD',
  SLEEPY: 'SLEEPY',
  SLEEP: 'SLEEPING',
  LOVE: 'LOVE',
  HEART: 'LOVE',
};

/**
 * Whether the stage can show *name*, which is the SDK's vocabulary and not
 * this file's own: a name the SDK offers and the stage cannot draw is a hole
 * in the page, so a test holds the two together.
 *
 * @param {string} name
 * @returns {boolean}
 */
export function knowsExpression(name) {
  return Object.hasOwn(RESOLVES_TO, String(name).trim().toUpperCase());
}

/**
 * The heart's outline, as the radius of the curve at each angle around it.
 *
 * Solved once from the curve the firmware uses -- (x^2+y^2-1)^3 = x^2 y^3 --
 * by walking out along each ray until the sign changes.
 *
 * @returns {number[]}
 */
function heartOutline() {
  const steps = 128;
  const inside = (x, y) => {
    const round = x * x + y * y - 1;
    return round * round * round - x * x * y * y * y <= 0;
  };
  const radii = [];
  for (let step = 0; step < steps; step += 1) {
    const angle = (step / steps) * Math.PI * 2;
    const dx = Math.cos(angle);
    const dy = Math.sin(angle);
    let low = 0;
    let high = 2;
    for (let pass = 0; pass < 40; pass += 1) {
      const mid = (low + high) / 2;
      if (inside(dx * mid, dy * mid)) {
        low = mid;
      } else {
        high = mid;
      }
    }
    radii.push(low);
  }
  return radii;
}

const HEART = heartOutline();

/**
 * A hue, a saturation and a brightness, as the channels a canvas wants.
 *
 * @param {{hue: number, sat: number, bri: number}} state
 * @returns {string} An `r,g,b` triple, ready to drop into an `rgba(...)`.
 */
function litBy({ hue, sat, bri }) {
  const turn = (((hue % 360) + 360) % 360) / 60;
  const drop = sat;
  const wave = drop * (1 - Math.abs((turn % 2) - 1));
  const order = [
    [drop, wave, 0],
    [wave, drop, 0],
    [0, drop, wave],
    [0, wave, drop],
    [wave, 0, drop],
    [drop, 0, wave],
  ][Math.floor(turn) % 6];
  return order
    .map((channel) => Math.min(255, Math.round((channel + 1 - sat) * 255 * bri)))
    .join(',');
}

/**
 * Draw the lit body: a core at full strength, fading to nothing across the
 * glow. Scaled about its own middle, so `sx` and `sy` stretch the fade with it.
 *
 * @param {CanvasRenderingContext2D} ctx
 * @param {object} state
 */
function drawBody(ctx, state) {
  const glow = Math.max(GLOW_WIDTH * state.glow, 1);
  const edge = CORE + glow;
  const lit = litBy(state);

  ctx.save();
  ctx.translate(PANEL / 2 + state.cx * RADIUS, PANEL / 2 + state.cy * RADIUS);
  ctx.scale(Math.max(state.sx, 1e-3), Math.max(state.sy, 1e-3));
  const fade = ctx.createRadialGradient(0, 0, 0, 0, 0, edge);
  fade.addColorStop(0, `rgba(${lit},1)`);
  fade.addColorStop(CORE / edge, `rgba(${lit},1)`);
  fade.addColorStop(1, `rgba(${lit},0)`);
  ctx.fillStyle = fade;
  ctx.beginPath();
  ctx.arc(0, 0, edge, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

/**
 * Draw the heart, for the one expression that is a shape rather than a body.
 *
 * @param {CanvasRenderingContext2D} ctx
 * @param {object} state
 */
function drawHeart(ctx, state) {
  const wide = HEART_SIZE * Math.max(state.sx, 1e-3);
  const tall = HEART_SIZE * Math.max(state.sy, 1e-3);

  ctx.save();
  ctx.translate(
    PANEL / 2 + state.cx * RADIUS,
    PANEL / 2 + (state.cy + HEART_DROP) * RADIUS
  );
  ctx.beginPath();
  HEART.forEach((radius, step) => {
    const angle = (step / HEART.length) * Math.PI * 2;
    const x = Math.cos(angle) * radius * wide;
    // Up on the panel is down in the curve's own frame.
    const y = -Math.sin(angle) * radius * tall;
    if (step === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.closePath();
  ctx.shadowColor = `rgba(${litBy({ hue: 330, sat: 0.62, bri: state.bri })},1)`;
  ctx.shadowBlur = 40;
  ctx.fillStyle = `rgba(${litBy(state)},1)`;
  ctx.fill();
  ctx.fill();
  ctx.restore();
}

/**
 * Draw the lid: a circle wider than the panel, coming down over whatever is
 * lit. Black rather than a cut-out, which is the same picture on a panel that
 * is black to begin with.
 *
 * @param {CanvasRenderingContext2D} ctx
 * @param {object} state
 */
function drawLid(ctx, state) {
  const radius = LID_RADIUS * 0.5 * (state.sx + state.sy);
  const middle = PANEL / 2 + state.cy * RADIUS;
  const down = middle - PANEL / 2 - radius + state.clid * (PANEL / 2 + radius);
  const across = PANEL / 2 + state.cx * RADIUS;
  const inner = Math.max(radius - LID_EDGE / 2, 0);
  const outer = radius + LID_EDGE / 2;

  const edge = ctx.createRadialGradient(across, down, inner, across, down, outer);
  edge.addColorStop(0, 'rgba(0,0,0,1)');
  edge.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = edge;
  ctx.fillRect(0, 0, PANEL, PANEL);
}

/**
 * Where an expression has got to, *elapsed* milliseconds in.
 *
 * @param {{keys: [number, number, object][]}} expression
 * @param {number} elapsed
 * @returns {object}
 */
function stateAt(expression, elapsed) {
  const settled = [];
  let carried = { ...REST };
  for (const [at, ease, changes] of expression.keys) {
    carried = { ...carried, ...changes };
    settled.push({ at, ease, state: carried });
  }

  const next = settled.findIndex(({ at }) => at > elapsed);
  if (next < 1) {
    return settled[next === 0 ? 0 : settled.length - 1].state;
  }
  const from = settled[next - 1];
  const to = settled[next];
  const through = EASE[to.ease]((elapsed - from.at) / (to.at - from.at));

  const state = {};
  for (const field of Object.keys(REST)) {
    if (field === 'hue') {
      // The short way round, so nothing crosses the greens on its way.
      const turn = (((to.state.hue - from.state.hue + 540) % 360) + 360) % 360;
      state.hue = from.state.hue + (turn - 180) * through;
    } else {
      state[field] = from.state[field] + (to.state[field] - from.state[field]) * through;
    }
  }
  return state;
}

/**
 * Put a face on the robot's head.
 *
 * @param {import('three').Object3D} model The posed model root.
 * @param {Map<string, {node: import('three').Object3D}>} joints As `readJoints` returns them.
 * @returns {{show: (name: string) => void, update: (now: number) => void, dispose: () => void}}
 */
export function mountFace(model, joints) {
  const { head, up, eye, facing, across } = headAnchor(model, joints);

  const canvas = document.createElement('canvas');
  canvas.width = PANEL;
  canvas.height = PANEL;
  const ctx = canvas.getContext('2d');
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;

  const screen = new Mesh(
    new CircleGeometry((across * SCREEN_ACROSS) / 2, 48),
    // Unlit: a screen makes its own light, and shading it would read as paint.
    new MeshBasicMaterial({ map: texture, toneMapped: false })
  );
  screen.name = 'face_screen';
  // Furniture the stage hung on the head, not part of the head to be measured.
  screen.userData.stageDressing = true;
  screen.position.copy(eye).addScaledVector(facing, across * SCREEN_STANDOFF);
  // A circle faces its own +Z, so aiming that at the way the plate looks is
  // what turns the screen out of the head.
  screen.quaternion.setFromRotationMatrix(
    new Matrix4().lookAt(new Vector3(), facing.clone().negate(), up)
  );
  head.add(screen);

  let showing = EXPRESSIONS.IDLE;
  let startedAt = null;
  let drawnAt = null;

  return {
    /**
     * Show *name*, which is any expression the SDK offers.
     *
     * @param {string} name
     */
    show: (name) => {
      const wanted = RESOLVES_TO[String(name).trim().toUpperCase()];
      if (wanted === undefined) {
        throw new Error(`the robot has no face called "${name}"`);
      }
      showing = EXPRESSIONS[wanted];
      startedAt = null;
      drawnAt = null;
    },
    /**
     * Carry the expression to *now*, and repaint if that moved it.
     *
     * @param {number} now Milliseconds, as `requestAnimationFrame` gives them.
     */
    update: (now) => {
      if (startedAt === null) {
        startedAt = now;
      }
      // The last keyframe is where an expression stays until the next command,
      // so the clock stops there rather than looping.
      const ends = showing.keys[showing.keys.length - 1][0];
      const elapsed = Math.min(now - startedAt, ends);
      if (elapsed === drawnAt) {
        return;
      }
      drawnAt = elapsed;

      const state = stateAt(showing, elapsed);
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, PANEL, PANEL);
      if (showing.heart === true) {
        drawHeart(ctx, state);
      } else {
        drawBody(ctx, state);
      }
      if (state.clid > 0.001) {
        drawLid(ctx, state);
      }
      texture.needsUpdate = true;
    },
    dispose: () => {
      screen.removeFromParent();
      screen.geometry.dispose();
      screen.material.dispose();
      texture.dispose();
    },
  };
}
