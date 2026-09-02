// Python itself, in the page: the console's commands are run by the SDK.
//
// Nothing here knows how the robot moves. It loads the interpreter, puts the
// real palmimo_sdk sources in front of it, and hands the stage whatever the
// SDK computed -- so a motion the SDK gains is a motion this console can run,
// and one it tunes is one this console shows differently, with no change here.
const PYODIDE_INDEX = '/pyodide/';
const PAYLOAD_URL = '/python/palmimo-sdk.json';
// Where the SDK's sources are mounted inside the interpreter's own filesystem.
const LIBRARY_DIR = '/lib/palmimo';
// What the page runs before handing the prompt over, and what it shows above
// the prompt: the same lines, from here, so the two cannot drift. They are the
// opening of any script that drives this robot, which is the point of showing
// them rather than a sentence about them.
export const SESSION_PREAMBLE = [
  'from palmimo_sdk import NeckYawDegrees, Palmimo',
  'from stage_bridge import StageCamera, StageDisplay, StageMic',
  'robot = Palmimo(camera=StageCamera(), display=StageDisplay(), mic=StageMic())',
  'robot.connect()',
];
// The site's own wiring, which is not the reader's usage and is not shown.
const BRIDGE_IMPORT = 'import stage_bridge';

// The frames the interpreter's own REPL adds above the reader's command. They
// are the plumbing that ran the line, not the line, and a reader tracking down
// a mistake in their own code should not have to skip past them.
const PLUMBING_FRAME = /^ {2}File "[^"]*\/pyodide\/console\.py"/;

/**
 * A traceback with the console's own frames taken out.
 *
 * @param {string} traceback
 * @returns {string}
 */
export function readableTraceback(traceback) {
  const kept = [];
  let dropping = false;
  for (const line of traceback.split('\n')) {
    if (line.startsWith('  File "')) {
      dropping = PLUMBING_FRAME.test(line);
    } else if (dropping && !line.startsWith('    ')) {
      // A frame's own source and carets are indented under it; anything else
      // has left the frame, and the exception's message is the usual case.
      dropping = false;
    }
    if (!dropping) {
      kept.push(line);
    }
  }
  return kept.join('\n');
}

/**
 * Start the interpreter, mount the SDK, and open a REPL over it.
 *
 * Split from the loading in {@link loadRuntime} so the part that can break --
 * whether the SDK still imports and computes under a wasm interpreter -- can
 * be run outside a browser.
 *
 * @param {any} pyodide A loaded Pyodide instance.
 * @param {Record<string, string>} payload Module path -> Python source.
 * @param {(text: string) => void} onOutput Called with anything the code prints.
 */
export function bootstrap(pyodide, payload, onOutput) {
  for (const [name, source] of Object.entries(payload)) {
    const target = `${LIBRARY_DIR}/${name}`;
    pyodide.FS.mkdirTree(target.slice(0, target.lastIndexOf('/')));
    pyodide.FS.writeFile(target, source);
  }
  pyodide.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(LIBRARY_DIR)})`);
  pyodide.runPython(BRIDGE_IMPORT);
  pyodide.runPython(SESSION_PREAMBLE.join('\n'));

  const { PyodideConsole, repr_shorten: shorten } = pyodide.pyimport('pyodide.console');
  const repl = PyodideConsole(pyodide.globals);
  repl.stdout_callback = onOutput;
  repl.stderr_callback = onOutput;

  // Kept as callables rather than source re-parsed per frame: the stage asks
  // for one of these sixty times a second.
  const stepOnce = pyodide.runPython('lambda: stage_bridge.joint_angles(robot.step())');
  const currentAngles = pyodide.runPython('lambda: stage_bridge.joint_angles(robot.positions)');
  const currentMotion = pyodide.runPython('lambda: robot.motion');
  const currentDt = pyodide.runPython('lambda: robot.dt');

  function unwrap(call) {
    const proxy = call();
    const angles = proxy.toJs({ dict_converter: Object.fromEntries });
    proxy.destroy();
    return angles;
  }

  return {
    /**
     * Run one line of Python, as a prompt would.
     *
     * A command that raises is an answer, not a failure: it comes back as the
     * traceback Python wrote, for the console to show like any other output.
     *
     * @param {string} source
     * @returns {Promise<{value: string | null, error: string | null, incomplete: boolean}>}
     */
    run: async (source) => {
      const future = repl.push(source);
      if (future.syntax_check === 'incomplete') {
        future.destroy();
        return { value: null, error: null, incomplete: true };
      }
      try {
        const result = await future;
        // A statement evaluates to None and echoes nothing, the way it does at
        // a Python prompt; only an expression's value is worth showing.
        const value = result === undefined ? null : shorten(result);
        // Anything Pyodide didn't implicitly convert comes back as a PyProxy
        // that outlives `future` and must be freed on its own.
        if (typeof result?.destroy === 'function') {
          result.destroy();
        }
        return { value, error: null, incomplete: false };
      } catch (error) {
        // The rejection carries the traceback Python formatted; the future
        // itself is spent by the time we get here, so it is not read again.
        const traceback = error.message ?? String(error);
        return { value: null, error: readableTraceback(traceback), incomplete: false };
      } finally {
        future.destroy();
      }
    },
    /** Advance one control cycle. @returns {Record<string, number>} radians per joint. */
    step: () => unwrap(stepOnce),
    /** The pose as it stands, without advancing. @returns {Record<string, number>} */
    angles: () => unwrap(currentAngles),
    /** The motion running now, or "idle" at rest. @returns {string} */
    motion: () => currentMotion(),
    /**
     * Seconds one {@link step} covers, as the facade paces it.
     *
     * Read per frame rather than once: the reader can rebind `robot` at the
     * prompt, and a new one may run at a different rate.
     *
     * @returns {number}
     */
    dt: () => currentDt(),
  };
}

/**
 * Fetch the interpreter and the SDK, then {@link bootstrap} them.
 *
 * @param {(text: string) => void} onOutput
 * @param {(stage: string) => void} [onProgress]
 */
export async function loadRuntime(onOutput, onProgress) {
  onProgress?.('Starting Python');
  const [{ loadPyodide }, payload] = await Promise.all([
    import(/* @vite-ignore */ `${PYODIDE_INDEX}pyodide.mjs`),
    fetch(PAYLOAD_URL).then((response) => {
      if (!response.ok) {
        throw new Error(`The SDK sources are not being served (${response.status}).`);
      }
      return response.json();
    }),
  ]);
  const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX });
  onProgress?.('Loading the SDK');
  return bootstrap(pyodide, payload, onOutput);
}
