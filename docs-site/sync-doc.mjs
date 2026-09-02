// Renders doc/**/*.md into Starlight content pages under src/content/docs/.
// Wired as `prebuild` and `predev`, so the site cannot be built from a
// hand-written copy of doc/ that has drifted from it.
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const REPO_BLOB_BASE =
  process.env.PALMIMO_DOC_BLOB_BASE ??
  'https://github.com/Jizai-inc/palmimo-devkit/blob/main';

// Registers every page this script is allowed to render, and the position
// each one takes in its Starlight sidebar group. A doc page missing here
// fails the build instead of landing at an unreviewed sidebar position.
const SIDEBAR_ORDER = {
  'guides/raspberry-pi-setup.md': 1,
  'guides/installation.md': 2,
  'guides/controlling-motions.md': 3,
  'guides/mcp-server.md': 4,
  'guides/motion-development-guide.md': 5,
  'guides/releasing.md': 6,
  'reference/api-reference.md': 1,
  'reference/motions.md': 2,
  'explanation/architecture.md': 1,
  'explanation/motion-system.md': 2,
};

const DOCS_SITE_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(DOCS_SITE_DIR, '..');
const DOC_ROOT = path.join(REPO_ROOT, 'doc');
const DOC_IMAGES_DIR = path.join(DOC_ROOT, 'images');
const CONTENT_DOCS_DIR = path.join(DOCS_SITE_DIR, 'src', 'content', 'docs');
const PUBLIC_DIR = path.join(DOCS_SITE_DIR, 'public');
const PUBLIC_IMAGES_DIR = path.join(PUBLIC_DIR, 'images');
const SITE_PAGES_DIR = path.join(DOCS_SITE_DIR, 'src', 'site-pages');
// The console runs the real SDK, so the SDK's sources are an asset this site
// publishes: they are read straight out of the package rather than copied into
// the site, which is what keeps the console from drifting into a second
// implementation of the robot.
const SDK_PACKAGE_DIR = path.join(REPO_ROOT, 'packages', 'palmimo_sdk', 'palmimo_sdk');
const SITE_PYTHON_DIR = path.join(DOCS_SITE_DIR, 'src', 'python');
const PUBLIC_PYTHON_DIR = path.join(PUBLIC_DIR, 'python');
const SDK_PAYLOAD_FILE = path.join(PUBLIC_PYTHON_DIR, 'palmimo-sdk.json');
// Python's own runtime, as a wasm build. Its files cannot be bundled the way a
// JavaScript dependency is -- the interpreter fetches them at load -- so they
// are copied out of node_modules and served from this site's own origin.
const PYODIDE_DIST_DIR = path.join(DOCS_SITE_DIR, 'node_modules', 'pyodide');
const PUBLIC_PYODIDE_DIR = path.join(PUBLIC_DIR, 'pyodide');
const PYODIDE_RUNTIME_FILES = [
  'pyodide.mjs',
  'pyodide.asm.js',
  'pyodide.asm.wasm',
  'python_stdlib.zip',
  'pyodide-lock.json',
];
// The extensions Starlight renders a page from. A hand-written site page may be
// either; a page rendered out of doc/ is always Markdown.
const PAGE_EXTENSIONS = ['.md', '.mdx'];

// A fenced code block, ``` or ~~~ delimited, matched non-greedily so two
// separate blocks in one file stay separate. A fence indented under a list
// item is still a code sample, so leading space is allowed on both fences.
// Not the same shape as FENCED_CODE in tests/contracts/, which reads prose for
// headings: this admits a fence longer than three characters, and an unclosed
// fence is no block at all rather than one running to end of file -- treating a
// typo as a block would hide every link below it from the rewriter.
const FENCE_RE = /^[ \t]*([`~]{3,})[^\n]*\n[\s\S]*?^[ \t]*\1[^\n]*$/gm;
// A backtick code span. Its delimiter run is matched on both sides so a span
// written with doubled backticks survives, and it is applied after fences so
// the backticks it sees are never a fence's own. A span may wrap across lines
// but never past a blank line, which ends the paragraph holding it -- bounding
// it there stops one unpaired backtick from swallowing the rest of the page.
const CODE_SPAN_RE = /(`+)(?:(?!\n[ \t]*\n)[\s\S])*?\1(?!`)/g;
// A link or an image. The label admits one nested image link so a thumbnail --
// `[![alt](diagram.svg)](page.md)` -- is matched whole; without that the label
// ends at the image's own `]` and the outer target ships unrewritten.
const LINK_RE = /(!?\[(?:!\[[^\]]*\]\([^)]*\)|[^\]])*\])\(([^)]+)\)/g;
// An image held inside such a label, so its target is resolved too. Global, and
// anchored to neither end: a label may caption the image or hold more than one.
const NESTED_IMAGE_RE = /(!\[[^\]]*\])\(([^)]+)\)/g;
// Every way a hand-written site page names a route on this site: a Markdown
// link or image, a frontmatter hero action, and an HTML attribute in the hero.
const SITE_ROUTE_RES = [
  /\]\((\/[^)\s]*)/g,
  /^\s*(?:link|src):\s*(\/\S*)/gm,
  /\b(?:src|href)="(\/[^"]*)"/g,
];
// The optional title Markdown allows after a link target -- `(page.md "Title")`.
// Split off before resolving, or the title travels into the path and the author
// is told a file named `page.md "Title"` does not exist.
const LINK_TITLE_RE = /^(\S+)(\s+(?:"[^"]*"|'[^']*'))$/;
// Stand in for a code sample while links are rewritten around it; a page that
// spells one of these out is rejected rather than silently mangled.
const FENCE_TOKEN_PREFIX = '%%FENCE';
const FENCE_TOKEN_RE = /%%FENCE(\d+)%%/g;
const SPAN_TOKEN_PREFIX = '%%SPAN';
const SPAN_TOKEN_RE = /%%SPAN(\d+)%%/g;

function toPosix(p) {
  return p.split(path.sep).join('/');
}

function isInside(parentDir, target) {
  const rel = path.relative(parentDir, target);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export function rewriteLink(docRelSourcePath, target) {
  if (/^(https?:|mailto:)/.test(target)) {
    return target;
  }

  const hashIndex = target.indexOf('#');
  const pathPart = hashIndex === -1 ? target : target.slice(0, hashIndex);
  const anchor = hashIndex === -1 ? '' : target.slice(hashIndex);

  if (pathPart === '') {
    return target;
  }

  const sourceDir = path.dirname(path.join(DOC_ROOT, docRelSourcePath));
  const abs = path.resolve(sourceDir, pathPart);

  if (!fs.existsSync(abs) || !isInside(REPO_ROOT, abs)) {
    throw new Error(
      `Link target does not exist inside the repository: "${target}" linked from doc/${docRelSourcePath}`
    );
  }

  if (isInside(DOC_IMAGES_DIR, abs)) {
    const imagesRel = toPosix(path.relative(DOC_IMAGES_DIR, abs));
    return `/images/${imagesRel}${anchor}`;
  }

  if (isInside(DOC_ROOT, abs)) {
    if (path.extname(abs) !== '.md') {
      throw new Error(
        `Link target inside doc/ is neither a Markdown page nor an image: "${target}" linked from doc/${docRelSourcePath}`
      );
    }
    const docRel = toPosix(path.relative(DOC_ROOT, abs));
    if (!(docRel in SIDEBAR_ORDER)) {
      throw new Error(
        `"${docRel}" has no SIDEBAR_ORDER entry -- register its sidebar order before linking to it from doc/${docRelSourcePath}`
      );
    }
    const slug = docRel.slice(0, -'.md'.length);
    return `/${slug}/${anchor}`;
  }

  const repoRel = toPosix(path.relative(REPO_ROOT, abs));
  return `${REPO_BLOB_BASE}/${repoRel}${anchor}`;
}

// A heading, a fenced block's opening/closing line, a list item, a
// blockquote, a table row, an HTML tag, an MDX import, or a thematic break --
// every shape of Markdown line that is not itself prose. Matched against a
// trimmed line, so indentation under a list item does not defeat a check.
const NON_PROSE_LINE_RE =
  /^(?:#{1,6}(?:\s|$)|[-*+]\s|\d+\.\s|>|\|)|^(?:-{3,}|\*{3,}|_{3,})$|^<\/?[a-zA-Z]|^import\s/;
const FENCE_START_RE = /^(`{3,}|~{3,})/;

// Strips a Markdown link/image/code-span/emphasis marker down to the text it
// carries, so a page's opening paragraph reads like prose in a meta tag
// rather than like Markdown source.
function stripInlineMarkdown(text) {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/(`+)([\s\S]+?)\1/g, '$2')
    .replace(/\*\*([\s\S]+?)\*\*/g, '$1')
    // _, __, and single * cannot open or close emphasis touching a word
    // character, or snake_case identifiers and *args/**kwargs get read as markers.
    .replace(/(?<!\w)__(?!_)([\s\S]+?)__(?!\w)/g, '$1')
    .replace(/(?<!\w)\*(?!\*)([\s\S]+?)\*(?!\w)/g, '$1')
    .replace(/(?<!\w)_(?!_)([\s\S]+?)_(?!\w)/g, '$1');
}

const DESCRIPTION_MAX_LENGTH = 160;
// Leaves room for the appended ellipsis while keeping the cut at a word
// boundary rather than mid-word.
const DESCRIPTION_TRUNCATE_AT = 157;

// Derives a one-line meta description from a page body: the first prose
// paragraph, Markdown syntax stripped, collapsed to one line and capped in
// length. Returns null when the page opens with nothing but headings, code,
// lists, or other non-prose blocks (frontmatter then omits `description`
// rather than emitting an empty one).
export function descriptionFor(body) {
  const lines = body.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();
    if (line === '') {
      i++;
      continue;
    }
    const fenceMatch = FENCE_START_RE.exec(line);
    if (fenceMatch) {
      const fenceRe = new RegExp(`^${fenceMatch[1][0]}{3,}`);
      i++;
      while (i < lines.length && !fenceRe.test(lines[i].trim())) {
        i++;
      }
      i++;
      continue;
    }
    if (NON_PROSE_LINE_RE.test(line)) {
      i++;
      continue;
    }
    break;
  }

  if (i >= lines.length) {
    return null;
  }

  const paragraph = [];
  while (i < lines.length && lines[i].trim() !== '') {
    paragraph.push(lines[i]);
    i++;
  }

  const collapsed = stripInlineMarkdown(paragraph.join(' ')).replace(/\s+/g, ' ').trim();
  if (collapsed === '') {
    return null;
  }
  if (collapsed.length <= DESCRIPTION_MAX_LENGTH) {
    return collapsed;
  }

  const cut = collapsed.slice(0, DESCRIPTION_TRUNCATE_AT);
  const lastSpace = cut.lastIndexOf(' ');
  return `${lastSpace > 0 ? cut.slice(0, lastSpace) : cut}…`;
}

export function frontmatterFor(docRelPath, h1Text, description, lastUpdated) {
  const order = SIDEBAR_ORDER[docRelPath];
  if (order === undefined) {
    throw new Error(`"${docRelPath}" has no SIDEBAR_ORDER entry -- register its sidebar order before generating it`);
  }
  const editUrl = `${REPO_BLOB_BASE}/doc/${docRelPath}`;
  const lines = ['---', `title: ${JSON.stringify(h1Text)}`];
  if (description) {
    lines.push(`description: ${JSON.stringify(description)}`);
  }
  lines.push('sidebar:', `  order: ${order}`);
  if (lastUpdated) {
    lines.push(`lastUpdated: ${lastUpdated}`);
  }
  lines.push(`editUrl: ${editUrl}`, '---');
  return `${lines.join('\n')}\n`;
}

function stripH1(content, docRelPath) {
  const lines = content.split('\n');
  if (!lines[0].startsWith('# ')) {
    throw new Error(`"doc/${docRelPath}" does not start with an H1 heading ("# ...") on its first line`);
  }
  const h1Text = lines[0].slice(2).trim();
  let rest = lines.slice(1);
  if (rest[0] === '') {
    rest = rest.slice(1);
  }
  return { h1Text, body: rest.join('\n') };
}

export function rewriteLinksInBody(docRelPath, body) {
  for (const reserved of [FENCE_TOKEN_PREFIX, SPAN_TOKEN_PREFIX]) {
    if (body.includes(reserved)) {
      throw new Error(
        `"doc/${docRelPath}" contains ${reserved}, which this script reserves to stand in for a code sample`
      );
    }
  }

  const stash = (text, pattern, prefix) => {
    const held = [];
    const withoutMatches = text.replace(pattern, (match) => {
      const token = `${prefix}${held.length}%%`;
      held.push(match);
      return token;
    });
    return { held, text: withoutMatches };
  };

  const fences = stash(body, FENCE_RE, FENCE_TOKEN_PREFIX);
  const spans = stash(fences.text, CODE_SPAN_RE, SPAN_TOKEN_PREFIX);

  const resolveTarget = (target) => {
    const [, pathPart = target, title = ''] = LINK_TITLE_RE.exec(target) ?? [];
    return `${rewriteLink(docRelPath, pathPart)}${title}`;
  };

  const rewritten = spans.text.replace(LINK_RE, (whole, label, target) => {
    const resolvedLabel = label.replace(
      NESTED_IMAGE_RE,
      (_, imageLabel, imageTarget) => `${imageLabel}(${resolveTarget(imageTarget)})`
    );
    return `${resolvedLabel}(${resolveTarget(target)})`;
  });

  return rewritten
    .replace(SPAN_TOKEN_RE, (_, index) => spans.held[Number(index)])
    .replace(FENCE_TOKEN_RE, (_, index) => fences.held[Number(index)]);
}

function listFilesRecursive(dir) {
  if (!fs.existsSync(dir)) {
    return [];
  }
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...listFilesRecursive(abs));
    } else if (entry.isFile()) {
      out.push(abs);
    }
  }
  return out.sort();
}

function copyTree(srcDir, destDir, sourceByOutput) {
  for (const srcFile of listFilesRecursive(srcDir)) {
    const relPath = path.relative(srcDir, srcFile);
    const destFile = path.join(destDir, relPath);
    fs.mkdirSync(path.dirname(destFile), { recursive: true });
    fs.copyFileSync(srcFile, destFile);
    sourceByOutput?.set(destFile, fs.readFileSync(srcFile, 'utf8'));
  }
}

let shallowRepoCache;

// Whether REPO_ROOT is a shallow checkout, computed once per run (not once
// per page) and cached for the rest of the process.
function isShallowRepository() {
  if (shallowRepoCache === undefined) {
    try {
      shallowRepoCache =
        execFileSync('git', ['rev-parse', '--is-shallow-repository'], {
          cwd: REPO_ROOT,
          encoding: 'utf8',
        }).trim() === 'true';
    } catch {
      // No usable git here at all -- a tarball export, or a host without git.
      // Treated as shallow: a date we cannot read is better absent than wrong.
      shallowRepoCache = true;
    }
  }
  return shallowRepoCache;
}

// The source page's last commit date, for the frontmatter's `lastUpdated`.
// Undefined on a shallow checkout: the grafted tip commit has no parent, so
// `git log -1 -- <path>` diffs it against the empty tree, matches every
// tracked file, and would stamp every page with that one commit's date
// instead of each page's real one. Also undefined (rather than thrown) for a
// file `git` has never seen.
export function lastCommitDate(absPath, isShallow = isShallowRepository()) {
  if (isShallow) {
    return undefined;
  }
  try {
    const out = execFileSync('git', ['log', '-1', '--format=%cI', '--', absPath], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
    }).trim();
    return out === '' ? undefined : out;
  } catch {
    return undefined;
  }
}

function processDocFile(absPath) {
  const docRelPath = toPosix(path.relative(DOC_ROOT, absPath));
  const raw = fs.readFileSync(absPath, 'utf8');
  const { h1Text, body } = stripH1(raw, docRelPath);
  const rewrittenBody = rewriteLinksInBody(docRelPath, body);
  const frontmatter = frontmatterFor(docRelPath, h1Text, descriptionFor(body), lastCommitDate(absPath));

  const outPath = path.join(CONTENT_DOCS_DIR, docRelPath);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, `${frontmatter}\n${rewrittenBody}`, 'utf8');

  return { outPath, raw };
}

// A pass-through http(s) link (e.g. a GitHub issue reference already written in
// doc/**) is copied verbatim, so it can legitimately carry the repo's GitHub org
// URL without having been built from REPO_BLOB_BASE. Exempt a match only when it
// already existed, byte-for-byte, in the source of the very page it appears on --
// checking against every source at once would let any one page's URL excuse it
// everywhere.
function checkNoStrayGithubLinks(sourceByOutput) {
  const orgUrlRe = /https:\/\/github\.com\/Jizai-inc\/[^\s)"'<>\]]+/g;
  for (const filePath of listFilesRecursive(CONTENT_DOCS_DIR)) {
    if (!PAGE_EXTENSIONS.includes(path.extname(filePath))) {
      continue;
    }
    const source = sourceByOutput.get(filePath);
    if (source === undefined) {
      throw new Error(`"${filePath}" was not written by this script -- it has no source to check against`);
    }
    const content = fs.readFileSync(filePath, 'utf8');
    for (const match of content.matchAll(orgUrlRe)) {
      const url = match[0];
      if (url.startsWith(REPO_BLOB_BASE) || source.includes(url)) {
        continue;
      }
      throw new Error(`Unexpected GitHub org URL not built from REPO_BLOB_BASE: "${url}" in ${filePath}`);
    }
  }
}

// A hand-written site page is copied verbatim, so nothing resolves the routes
// it names the way rewriteLink resolves a doc/ link. Check them against what
// this run produced, so renaming a doc page breaks the build rather than
// leaving the splash pointing at a 404.
function checkSitePageRoutes() {
  for (const srcFile of listFilesRecursive(SITE_PAGES_DIR)) {
    const content = fs.readFileSync(srcFile, 'utf8');
    for (const routeRe of SITE_ROUTE_RES) {
      for (const [, route] of content.matchAll(routeRe)) {
        if (!routeExists(route)) {
          throw new Error(`"${route}" in ${srcFile} is not a route this site builds`);
        }
      }
    }
  }
}

export function routeExists(route) {
  const [withoutAnchor] = route.split('#');
  const slug = withoutAnchor.replace(/^\/+|\/+$/g, '');
  if (slug === '') {
    return true;
  }
  // A route carrying a file extension names an asset this site publishes
  // verbatim out of public/ -- a rendered diagram, the 3D model -- rather than
  // a page the build generates from doc/.
  if (path.extname(slug) !== '') {
    const asset = path.resolve(PUBLIC_DIR, slug);
    return isInside(PUBLIC_DIR, asset) && fs.existsSync(asset);
  }
  return PAGE_EXTENSIONS.some((ext) =>
    fs.existsSync(path.join(CONTENT_DOCS_DIR, `${slug}${ext}`))
  );
}

// Collects the Python the browser has to have on disk before it can
// `import palmimo_sdk`: the package itself, plus the site's own bridge module.
function writePythonPayload() {
  const payload = {};
  for (const dir of [SDK_PACKAGE_DIR, SITE_PYTHON_DIR]) {
    const base = dir === SDK_PACKAGE_DIR ? 'palmimo_sdk' : '';
    for (const absPath of listFilesRecursive(dir)) {
      if (path.extname(absPath) !== '.py') {
        continue;
      }
      const relPath = toPosix(path.relative(dir, absPath));
      payload[base === '' ? relPath : `${base}/${relPath}`] = fs.readFileSync(absPath, 'utf8');
    }
  }
  const modules = Object.keys(payload).length;
  if (modules === 0) {
    throw new Error(`No Python found under ${SDK_PACKAGE_DIR}; the console would load an empty SDK.`);
  }
  fs.mkdirSync(PUBLIC_PYTHON_DIR, { recursive: true });
  fs.writeFileSync(SDK_PAYLOAD_FILE, JSON.stringify(payload));
  return modules;
}

function copyPyodideRuntime() {
  if (!fs.existsSync(PYODIDE_DIST_DIR)) {
    throw new Error(`Pyodide is not installed at ${PYODIDE_DIST_DIR}; run npm install.`);
  }
  fs.mkdirSync(PUBLIC_PYODIDE_DIR, { recursive: true });
  for (const name of PYODIDE_RUNTIME_FILES) {
    fs.copyFileSync(path.join(PYODIDE_DIST_DIR, name), path.join(PUBLIC_PYODIDE_DIR, name));
  }
}

function main() {
  fs.rmSync(CONTENT_DOCS_DIR, { recursive: true, force: true });
  fs.mkdirSync(CONTENT_DOCS_DIR, { recursive: true });
  fs.rmSync(PUBLIC_IMAGES_DIR, { recursive: true, force: true });
  fs.mkdirSync(PUBLIC_IMAGES_DIR, { recursive: true });

  const sourceByOutput = new Map();

  const docFiles = listFilesRecursive(DOC_ROOT).filter(
    (p) => !isInside(DOC_IMAGES_DIR, p) && path.extname(p) === '.md'
  );
  for (const docFile of docFiles) {
    const { outPath, raw } = processDocFile(docFile);
    sourceByOutput.set(outPath, raw);
  }

  copyTree(DOC_IMAGES_DIR, PUBLIC_IMAGES_DIR);
  copyTree(SITE_PAGES_DIR, CONTENT_DOCS_DIR, sourceByOutput);

  checkNoStrayGithubLinks(sourceByOutput);
  checkSitePageRoutes();

  const modules = writePythonPayload();
  copyPyodideRuntime();

  console.log(
    `Synced ${docFiles.length} documentation pages from doc/, and ${modules} Python modules for the console.`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main();
}
