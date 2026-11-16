import assert from 'node:assert/strict';
import { test } from 'node:test';

import { knowsExpression } from './src/scripts/robot-face.js';

test('the stage rests at IDLE, which the SDK reaches through display.idle()', () => {
  assert.ok(knowsExpression('IDLE'));
  // Case and stray spacing come off a Python string the reader typed.
  assert.ok(knowsExpression(' idle '));
});

test('a name the robot has no face for is refused rather than guessed at', () => {
  assert.equal(knowsExpression('SMUG'), false);
});
