// SPDX-License-Identifier: Apache-2.0 is intentionally NOT applied here --
// this test ships with the docs site, not the palmimo_sdk package.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  descriptionFor,
  lastCommitDate,
  rewriteLink,
  rewriteLinksInBody,
  routeExists,
} from './sync-doc.mjs';

// Same expression sync-doc.mjs uses for REPO_BLOB_BASE, so this test stays
// correct when PALMIMO_DOC_BLOB_BASE is set.
const BLOB_BASE =
  process.env.PALMIMO_DOC_BLOB_BASE ??
  'https://github.com/Jizai-inc/palmimo-devkit/blob/main';

test('same-page anchor is returned unchanged', () => {
  assert.equal(rewriteLink('guides/mcp-server.md', '#authentication'), '#authentication');
});

test('a same-directory doc page becomes a site-root path', () => {
  assert.equal(
    rewriteLink('guides/installation.md', 'raspberry-pi-setup.md'),
    '/guides/raspberry-pi-setup/'
  );
});

test('a doc page in another directory keeps its anchor', () => {
  assert.equal(
    rewriteLink('guides/controlling-motions.md', '../reference/api-reference.md#core-methods'),
    '/reference/api-reference/#core-methods'
  );
});

test('an image link becomes a /images/ path', () => {
  assert.equal(
    rewriteLink('explanation/motion-system.md', '../images/motor_layout.drawio.svg'),
    '/images/motor_layout.drawio.svg'
  );
});

test('a source file outside doc/ becomes a GitHub blob link', () => {
  assert.equal(
    rewriteLink('guides/controlling-motions.md', '../../packages/palmimo_sdk/palmimo_sdk/robot.py'),
    `${BLOB_BASE}/packages/palmimo_sdk/palmimo_sdk/robot.py`
  );
});

test('a repo-root file outside doc/ keeps its anchor on the blob link', () => {
  assert.equal(
    rewriteLink('guides/controlling-motions.md', '../../README.md#safety'),
    `${BLOB_BASE}/README.md#safety`
  );
});

test('a target that does not exist throws', () => {
  assert.throws(
    () => rewriteLink('guides/installation.md', 'nonexistent-page.md'),
    /does not exist/
  );
});

test('a link inside a code span is left as written', () => {
  const body = 'Write `[AGENTS.md](../../AGENTS.md)` to link out of doc/.';
  assert.equal(rewriteLinksInBody('guides/installation.md', body), body);
});

test('a code span delimited by doubled backticks is left as written', () => {
  const body = 'A span holding a backtick: ``[x](../../AGENTS.md) ` `` stays put.';
  assert.equal(rewriteLinksInBody('guides/installation.md', body), body);
});

test('a link inside a fenced block is left as written', () => {
  const body = '```md\n[AGENTS.md](../../AGENTS.md)\n```';
  assert.equal(rewriteLinksInBody('guides/installation.md', body), body);
});

test('a code span that wraps a line does not swallow the prose after it', () => {
  // The closing backtick opens the next line unpaired: pairing it with a later
  // span on that line would hide the link between them from the rewriter.
  const body = 'Prefix with `env -u\nTOKEN`) -- see [the guide](mcp-server.md) and `stop()`.';
  assert.equal(
    rewriteLinksInBody('guides/installation.md', body),
    'Prefix with `env -u\nTOKEN`) -- see [the guide](/guides/mcp-server/) and `stop()`.'
  );
});

test('a link inside a code span that wraps a line is left as written', () => {
  const body = 'Run `uv run\n[AGENTS.md](../../AGENTS.md)` first.';
  assert.equal(rewriteLinksInBody('guides/installation.md', body), body);
});

test('a thumbnail rewrites both the image and the link around it', () => {
  assert.equal(
    rewriteLinksInBody(
      'guides/installation.md',
      '[![Layout](../images/motor_layout.drawio.svg)](../explanation/architecture.md)'
    ),
    '[![Layout](/images/motor_layout.drawio.svg)](/explanation/architecture/)'
  );
});

test('a thumbnail with a caption beside it rewrites the image too', () => {
  assert.equal(
    rewriteLinksInBody(
      'guides/installation.md',
      '[![Layout](../images/motor_layout.drawio.svg) see below](../explanation/architecture.md)'
    ),
    '[![Layout](/images/motor_layout.drawio.svg) see below](/explanation/architecture/)'
  );
});

test('a link title is kept and stays out of the resolved path', () => {
  assert.equal(
    rewriteLinksInBody('guides/installation.md', '[the guide](mcp-server.md "MCP server")'),
    '[the guide](/guides/mcp-server/ "MCP server")'
  );
});

test('a link beside a code span still rewrites', () => {
  assert.equal(
    rewriteLinksInBody('guides/installation.md', '`sample` then [AGENTS.md](../../AGENTS.md)'),
    `\`sample\` then [AGENTS.md](${BLOB_BASE}/AGENTS.md)`
  );
});

test('a site route naming a file published from public/ resolves', () => {
  assert.equal(routeExists('/models/palmimo.glb'), true);
  assert.equal(routeExists('/favicon.ico'), true);
});

test('a site route naming a file public/ does not hold is rejected', () => {
  assert.equal(routeExists('/models/absent.glb'), false);
});

test('a site route cannot reach outside public/ for an asset', () => {
  assert.equal(routeExists('/../package.json'), false);
});

test('descriptionFor takes the first paragraph as-is', () => {
  assert.equal(
    descriptionFor('Palmimo is a modular hexapod development kit.\n\nMore prose follows.'),
    'Palmimo is a modular hexapod development kit.'
  );
});

test('descriptionFor skips a heading and a fenced code block to find the first paragraph', () => {
  const body = [
    '## Quickstart',
    '',
    '```bash',
    'uv run python scripts/wave.py',
    '```',
    '',
    'Connect the robot before running any motion script.',
  ].join('\n');
  assert.equal(descriptionFor(body), 'Connect the robot before running any motion script.');
});

test('descriptionFor strips inline Markdown down to its text', () => {
  const body =
    'See [the guide](guides/installation.md) and run `robot.forward()` for **bold** and _emphasis_.';
  assert.equal(
    descriptionFor(body),
    'See the guide and run robot.forward() for bold and emphasis.'
  );
});

test('descriptionFor leaves snake_case identifiers alone while still stripping real emphasis', () => {
  const body =
    '`palmimo_sdk.mcp` exposes *the same* tools (forward, set_face, ...) that **leg_1_yaw** drives.';
  assert.equal(
    descriptionFor(body),
    'palmimo_sdk.mcp exposes the same tools (forward, set_face, ...) that leg_1_yaw drives.'
  );
});

test('descriptionFor truncates a long paragraph at a word boundary', () => {
  const body =
    'This paragraph is deliberately long enough to exceed the one hundred and sixty character ' +
    'limit so that the truncation logic has to cut it off cleanly at a word boundary instead of mid-word.';
  const result = descriptionFor(body);
  assert.ok(result.length <= 158);
  assert.ok(result.endsWith('…'));
  assert.ok(!result.slice(0, -1).endsWith(' '));
});

test('lastCommitDate is omitted on a shallow checkout', () => {
  const thisFile = fileURLToPath(import.meta.url);
  assert.equal(lastCommitDate(thisFile, true), undefined);
});
