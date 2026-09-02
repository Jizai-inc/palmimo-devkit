import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { knowsExpression } from './src/scripts/robot-face.js';

// The SDK's own list, read where it is written rather than copied here: the
// page runs that SDK, so the day it grows an expression is the day this page
// owes the robot a face for it.
const DISPLAY = new URL('../packages/palmimo_sdk/palmimo_sdk/io/display.py', import.meta.url);

function expressionsTheSdkOffers() {
  const source = readFileSync(DISPLAY, 'utf8');
  const table = /^EXPRESSIONS = \(\n([\s\S]*?)^\)/m.exec(source);
  assert.ok(table !== null, 'the SDK no longer declares EXPRESSIONS as a tuple literal');
  return [...table[1].matchAll(/"([A-Z_]+)"/g)].map(([, name]) => name);
}

test('every expression the SDK offers has a face on the stage', () => {
  const offered = expressionsTheSdkOffers();

  assert.ok(offered.length > 0, 'read no expressions out of the SDK');
  assert.deepEqual(
    offered.filter((name) => !knowsExpression(name)),
    []
  );
});

test('the stage rests at IDLE, which the SDK reaches through display.idle()', () => {
  assert.ok(knowsExpression('IDLE'));
  // Case and stray spacing come off a Python string the reader typed.
  assert.ok(knowsExpression(' idle '));
});

test('a name the robot has no face for is refused rather than guessed at', () => {
  assert.equal(knowsExpression('SMUG'), false);
});
