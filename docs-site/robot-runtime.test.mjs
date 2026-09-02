// The console's load-bearing claim: the SDK that ships to hardware is the SDK
// the page runs. Checked against the real interpreter and the real payload,
// because "it still imports under wasm" is exactly what a refactor breaks
// silently -- the site builds, and the console fails on the reader's first
// command.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { loadPyodide } from 'pyodide';
import { bootstrap, readableTraceback } from './src/scripts/robot-runtime.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const payload = JSON.parse(
  fs.readFileSync(path.join(HERE, 'public', 'python', 'palmimo-sdk.json'), 'utf8')
);

const printed = [];
const pyodide = await loadPyodide();
const runtime = bootstrap(pyodide, payload, (text) => printed.push(text));

const LEG_JOINTS = ['leg_1_yaw', 'leg_1_pitch1', 'leg_1_pitch2'];

test('the SDK boots and stands at neutral', () => {
  const angles = runtime.angles();
  assert.equal(Object.keys(angles).length, 20);
  for (const joint of [...LEG_JOINTS, 'neck_yaw', 'neck_pitch1']) {
    assert.equal(angles[joint], 0, `${joint} should stand at neutral`);
  }
});

test('the console\'s robot arrives with a camera the SDK would accept', async () => {
  // The claim the splash makes by showing StageCamera in its opening lines.
  // The contract is this page's own -- the SDK types the parameter as the
  // concrete HeadCamera and publishes no Protocol -- so the half that carries
  // any weight is the second assertion: the shape written down here is the
  // shape the robot's own camera has. Were it not, the page would be
  // demonstrating a seam that exists only on the page.
  await runtime.run('from stage_bridge import CameraSource');
  assert.equal((await runtime.run('isinstance(robot.camera, CameraSource)')).value, 'True');
  await runtime.run('from palmimo_sdk.io.camera import HeadCamera');
  assert.equal(
    (await runtime.run('isinstance(HeadCamera.__new__(HeadCamera), CameraSource)')).value,
    'True'
  );
  // connect() ran in the preamble, and it is what opens a peripheral.
  assert.equal((await runtime.run('robot.camera.is_open')).value, 'True');
});

test('a camera with no stage behind it reports an empty grab, not an error', async () => {
  // How the real HeadCamera reports a device it could not open, and the state
  // the console is in for as long as the model is still loading.
  assert.equal((await runtime.run('robot.camera.read()')).value, '(False, None)');
});

test('a frame comes back as bytes that know their own shape', async () => {
  // A stage of two pixels, standing in for the one the browser draws.
  globalThis.palmimoStage = {
    read: () => ({ width: 2, height: 1, data: new Uint8Array([1, 2, 3, 4, 5, 6]) }),
  };
  try {
    assert.equal((await runtime.run('ok, frame = robot.camera.read(); ok')).value, 'True');
    // Rows, then columns, then the three colour channels -- what a caller who
    // has met OpenCV expects to find, and what read() has to keep meaning.
    assert.equal((await runtime.run('frame.shape')).value, '(1, 2, 3)');
    assert.equal((await runtime.run('bytes(frame)')).value, String.raw`b'\x01\x02\x03\x04\x05\x06'`);
    // The drain's fan-out, which a face-tracking tool subscribes to.
    await runtime.run('seen = []');
    await runtime.run('robot.camera.add_consumer(lambda frame, at: seen.append(frame.shape))');
    await runtime.run('robot.camera.read()');
    assert.equal((await runtime.run('seen')).value, '[(1, 2, 3)]');
  } finally {
    delete globalThis.palmimoStage;
  }
});

test('a command the reader types moves the joints', async () => {
  const started = await runtime.run('robot.forward()');
  assert.deepEqual(started, { value: null, error: null, incomplete: false });

  let moved = {};
  for (let i = 0; i < 30; i += 1) {
    moved = runtime.step();
  }
  assert.ok(
    LEG_JOINTS.some((joint) => Math.abs(moved[joint]) > 0.01),
    'thirty control cycles of walking should have turned a leg'
  );
  // Radians: a servo that ran away would show up here as a leg wrapped
  // through the body rather than as a joint within its travel.
  for (const [joint, angle] of Object.entries(moved)) {
    assert.ok(Math.abs(angle) < Math.PI / 2, `${joint} turned ${angle} rad`);
  }
});

test('an expression echoes its value, a statement does not', async () => {
  assert.equal((await runtime.run('robot.motion')).value, "'forward'");
  assert.equal((await runtime.run('x = 2 + 2')).value, null);
  assert.equal((await runtime.run('x')).value, '4');
});

test('a non-primitive result is freed once its repr has been read', async () => {
  // __del__ fires the instant refcount hits zero (CPython, no gc needed), so
  // it distinguishes "destroyed after use" from "leaked until the tab closes".
  await runtime.run('class _Leak:');
  await runtime.run('    def __del__(self):');
  await runtime.run("        globals()['_freed'] = True");
  await runtime.run('');
  await runtime.run("_freed = False");
  await runtime.run('_Leak()');
  assert.equal((await runtime.run('_freed')).value, 'True');
});

test('the motion accessor tracks what the reader started', async () => {
  assert.equal(runtime.motion(), 'forward');
  await runtime.run('robot.stop()');
  assert.equal(runtime.motion(), 'idle');
});

test('the control period follows the robot at the prompt', async () => {
  assert.equal(runtime.dt(), 1 / 60);
  await runtime.run('robot = Palmimo(fps=30)');
  assert.equal(runtime.dt(), 1 / 30);
  await runtime.run('robot = Palmimo()');
});

test('print reaches the console output', async () => {
  printed.length = 0;
  await runtime.run('print(len(robot.positions), "servos")');
  assert.equal(printed.join('').trim(), '20 servos');
});

test('an unfinished block asks for the next line', async () => {
  assert.deepEqual(await runtime.run('for i in range(2):'), {
    value: null,
    error: null,
    incomplete: true,
  });
  assert.deepEqual(await runtime.run('    print(i)'), {
    value: null,
    error: null,
    incomplete: true,
  });
  printed.length = 0;
  await runtime.run('');
  assert.equal(printed.join('').trim(), '0\n1');
});

test('a mistake is reported rather than thrown', async () => {
  const { error } = await runtime.run('robot.fly()');
  assert.match(error, /AttributeError/);
});

test('a traceback shows the reader their own frames only', () => {
  const raw = [
    'Traceback (most recent call last):',
    '  File "/lib/python313.zip/pyodide/console.py", line 487, in _runcode_with_lock',
    '    return await self.runcode(source, code)',
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^',
    '  File "<console>", line 1, in <module>',
    'AttributeError: no such thing',
  ].join('\n');
  assert.equal(
    readableTraceback(raw),
    [
      'Traceback (most recent call last):',
      '  File "<console>", line 1, in <module>',
      'AttributeError: no such thing',
    ].join('\n')
  );
});
