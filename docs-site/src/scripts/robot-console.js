// The prompt under the stage: what the reader types, the robot does.
//
// This file is the wiring between the two -- it owns the transcript, the
// history, and the frame loop. What a command means is the SDK's business,
// and what the robot looks like is the stage's.
import { loadRuntime } from './robot-runtime.js';

const PROMPT = '>>>';
const CONTINUATION = '...';
// Ceiling on the cycles one frame may catch up on. A tab returning from the
// background owes minutes of them; the robot skips that time rather than
// replaying it at once.
const MAX_CATCHUP_CYCLES = 5;
// Lines of transcript kept. The box is a fixed size, so past this the oldest
// are already out of sight; dropping them keeps a long session from carrying
// the whole of itself around.
const MAX_TRANSCRIPT_LINES = 200;

/**
 * Wire a console's markup to a mounted stage.
 *
 * The interpreter is not fetched until the reader runs something: it is a few
 * megabytes, and a reader who came to look at the robot should not pay for it.
 *
 * @param {HTMLElement} root
 * @param {{pose: (angles: Record<string, number>) => void}} stage
 */
export function mountConsole(root, stage) {
  const log = root.querySelector('.console__log');
  const transcript = root.querySelector('.console__transcript');
  const form = root.querySelector('.console__form');
  const input = root.querySelector('.console__input');
  const caret = root.querySelector('.console__caret');
  const statusDot = root.querySelector('.console__status-dot');
  const statusLabel = root.querySelector('.console__status-label');
  const stopButton = root.querySelector('.console__ghost--stop');
  const clearButton = root.querySelector('.console__ghost--clear');
  const exampleButtons = root.querySelectorAll('.console__example');
  const history = [];
  let recalled = history.length;
  let runtime = null;
  let starting = null;
  let shownMotion = 'idle';
  let pending = Promise.resolve();
  let inFlight = 0;

  function write(text, kind) {
    const line = document.createElement('pre');
    line.className = `console__line console__line--${kind}`;
    line.textContent = text;
    transcript.append(line);
    while (transcript.childElementCount > MAX_TRANSCRIPT_LINES) {
      transcript.firstElementChild.remove();
    }
    log.scrollTop = log.scrollHeight;
    return line;
  }

  // A motion runs until stopped, so the dot is the only thing on the page
  // that says whether the robot is still going. Written only on change: the
  // frame loop asks sixty times a second and it is idle almost every time.
  function showMotion(motion) {
    if (motion === shownMotion) {
      return;
    }
    shownMotion = motion;
    const running = motion !== 'idle';
    statusDot.textContent = running ? '●' : '○';
    statusDot.classList.toggle('console__status-dot--active', running);
    statusLabel.textContent = motion;
    stopButton.disabled = !running;
  }

  async function start() {
    const note = write('Starting Python', 'note');
    try {
      runtime = await loadRuntime(
        (text) => write(text.replace(/\n$/, ''), 'output'),
        (phase) => {
          note.textContent = phase;
        }
      );
    } catch (error) {
      note.remove();
      // Rethrown with the context the caller cannot add: submit() owns what
      // the transcript says about a failure, wherever it came from.
      throw new Error(`Could not start Python: ${error.message}`);
    }
    note.remove();
    write('palmimo_sdk is running in this page. The robot is its output.', 'note');
    // From here the robot is the SDK's: every step is a control cycle, the
    // same call a connected driver would be writing to the servos. The clock
    // is the SDK's too -- one cycle per frame would run every gait at double
    // speed on a 120 Hz screen, and slow on a throttled tab.
    let last = performance.now();
    let owed = 0;
    const drive = (now) => {
      requestAnimationFrame(drive);
      const cycle = runtime.dt() * 1000;
      owed = Math.min(owed + (now - last), cycle * MAX_CATCHUP_CYCLES);
      last = now;
      while (owed >= cycle) {
        owed -= cycle;
        stage.pose(runtime.step());
      }
      showMotion(runtime.motion());
    };
    requestAnimationFrame(drive);
    return runtime;
  }

  async function submit(source) {
    history.push(source);
    recalled = history.length;
    write(`${caret.textContent} ${source}`, 'echo');
    try {
      starting ??= start();
      const ready = await starting;
      const { value, error, incomplete } = await ready.run(source);
      caret.textContent = incomplete ? CONTINUATION : PROMPT;
      if (value !== null) {
        write(value, 'output');
      }
      if (error !== null) {
        write(error, 'error');
      }
    } catch (failure) {
      // A failed start is not kept: without this the rejected promise answers
      // every later command with silence, and the prompt looks alive but dead.
      starting = null;
      write(failure.message, 'error');
    }
  }

  // One command at a time, in the order the reader asked for them. Two chips
  // clicked during the interpreter download would otherwise both reach the
  // interpreter, interleaving their echoes and closing an open block at the
  // wrong indent.
  function enqueue(source) {
    inFlight += 1;
    setBusy(true);
    pending = pending
      .then(() => submit(source))
      .catch((failure) => write(failure.message, 'error'))
      .finally(() => {
        inFlight -= 1;
        if (inFlight === 0) {
          setBusy(false);
        }
      });
  }

  function setBusy(busy) {
    input.disabled = busy;
    for (const button of exampleButtons) {
      button.disabled = busy;
    }
    if (!busy) {
      input.focus();
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const source = input.value;
    input.value = '';
    if (source.trim() !== '' || caret.textContent === CONTINUATION) {
      enqueue(source);
    }
  });

  input.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') {
      return;
    }
    event.preventDefault();
    recalled = Math.min(
      history.length,
      Math.max(0, recalled + (event.key === 'ArrowUp' ? -1 : 1))
    );
    input.value = history[recalled] ?? '';
  });

  for (const button of exampleButtons) {
    button.addEventListener('click', () => {
      input.focus();
      enqueue(button.textContent);
    });
  }

  // Routed through submit(), like a chip, so the transcript shows the reader
  // exactly what stopped the robot instead of a button click nothing echoes.
  stopButton.addEventListener('click', () => {
    input.focus();
    enqueue('robot.stop()');
  });

  clearButton.addEventListener('click', () => {
    transcript.replaceChildren();
    write('The transcript is clear. The robot kept whatever it was doing.', 'note');
  });
}
