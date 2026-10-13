// SPDX-License-Identifier: Apache-2.0 is intentionally NOT applied here --
// this test ships with the docs site, not the palmimo_sdk package.
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
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

test('long descriptions truncate at language-appropriate boundaries', async (t) => {
  const english =
    'This paragraph is deliberately long enough to exceed the one hundred and sixty character ' +
    'limit so that the truncation logic has to cut it off cleanly at a word boundary instead of mid-word.';
  for (const [name, body, expected] of [
    ['English', english, null],
    ['Japanese period', 'Palmimo はロボットです。'.repeat(14), 'Palmimo はロボットです。'.repeat(9) + '…'],
    ['Japanese comma', 'Palmimo はロボットです、'.repeat(14), 'Palmimo はロボットです、'.repeat(9) + '…'],
  ]) {
    await t.test(name, () => {
      const result = descriptionFor(body);
      assert.ok(result.length <= 158);
      assert.ok(result.endsWith('…'));
      assert.ok(!result.slice(0, -1).endsWith(' '));
      if (expected !== null) assert.equal(result, expected);
    });
  }
});

test('lastCommitDate is omitted on a shallow checkout', () => {
  const thisFile = fileURLToPath(import.meta.url);
  assert.equal(lastCommitDate(thisFile, true), undefined);
});

test('Japanese pages link to Japanese pages and share images', async (t) => {
  for (const [link, expected] of [
    ['mcp-server.md#認証', '/ja/guides/mcp-server/#認証'],
    ['../../images/motor_layout.drawio.svg', '/images/motor_layout.drawio.svg'],
    ['../../../README.md#safety', BLOB_BASE + '/README.md#safety'],
    ['../../guides/mcp-server.md', null],
  ]) {
    await t.test(link, () => {
      if (expected === null) {
        assert.throws(() => rewriteLink('ja/guides/installation.md', link));
      } else {
        assert.equal(rewriteLink('ja/guides/installation.md', link), expected);
      }
    });
  }
});


function docFixture(t) {
  const tmp = fileURLToPath(new URL('../tmp/', import.meta.url));
  fs.mkdirSync(tmp, { recursive: true });
  const root = fs.mkdtempSync(path.join(tmp, 'sync-doc-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const files = {
    'doc/guides/installation.md': '# Installation\n',
    'doc/guides/mcp-server.md': '# MCP server\n',
    'doc/ja/guides/installation.md': '# Installation (Japanese)\n',
    'doc/ja/guides/mcp-server.md': '# MCP server (Japanese)\n',
    'docs-site/src/site-pages/index.mdx': 'Splash\n',
    'docs-site/src/site-pages/ja/index.mdx': 'Japanese splash\n',
    'packages/palmimo_sdk/palmimo_sdk/__init__.py': '',
  };
  for (const name of ['pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json']) {
    files['docs-site/node_modules/pyodide/' + name] = '';
  }
  for (const [name, body] of Object.entries(files)) {
    const target = path.join(root, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, body);
  }
  const script = path.join(root, 'docs-site/sync-doc.mjs');
  fs.copyFileSync(fileURLToPath(new URL('./sync-doc.mjs', import.meta.url)), script);
  return { root, script };
}

test('Japanese links to untranslated English pages use the Japanese fallback route', (t) => {
  const { root, script } = docFixture(t);
  fs.unlinkSync(path.join(root, 'doc/ja/guides/mcp-server.md'));
  fs.writeFileSync(path.join(root, 'docs-site/src/site-pages/ja/index.mdx'), '[MCP](/ja/guides/mcp-server/)\n');
  fs.writeFileSync(path.join(root, 'doc/ja/guides/installation.md'), '# Installation\n\n[MCP](../../guides/mcp-server.md)\n');
  const result = spawnSync(process.execPath, [script], { cwd: root, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(fs.readFileSync(path.join(root, 'docs-site/src/content/docs/ja/guides/installation.md'), 'utf8').includes('[MCP](/ja/guides/mcp-server/)'));
});

test('documentation and splash links cannot switch to the other locale', async (t) => {
  for (const [file, body] of [
    ['doc/guides/installation.md', '# Installation\n\n[Japanese](../ja/guides/mcp-server.md)\n'],
    ['docs-site/src/site-pages/ja/index.mdx', '[English](/guides/mcp-server/)\n'],
    ['docs-site/src/site-pages/index.mdx', '[Japanese](/ja/guides/mcp-server/)\n'],
  ]) {
    await t.test(file, (t) => {
      const { root, script } = docFixture(t);
      fs.writeFileSync(path.join(root, file), body);
      const result = spawnSync(process.execPath, [script], { cwd: root, encoding: 'utf8' });
      assert.notEqual(result.status, 0);
    });
  }
});
