import assert from 'node:assert/strict';
import test from 'node:test';
import * as consoleModule from './src/scripts/robot-console.js';

test('console UI uses supplied labels while preserving motion identifiers', async (t) => {
  const labels = {
    idle: '待機の表示',
    starting: '起動の表示',
    loading: '読み込みの表示',
    ready: '準備完了の表示',
    cleared: '消去後の表示',
    startError: '起動失敗: ',
  };
  for (const [state, value, expected] of [
    ['motion', 'idle', labels.idle],
    ['motion', 'forward', 'forward'],
    ['progress', 'Starting Python', labels.starting],
    ['progress', 'Loading the SDK', labels.loading],
    ['ready', '', labels.ready],
    ['cleared', '', labels.cleared],
    ['startError', 'RuntimeError', labels.startError + 'RuntimeError'],
  ]) {
    await t.test(`${state} ${value}`, () => {
      assert.equal(consoleModule.consoleText(labels, state, value), expected);
    });
  }
});
