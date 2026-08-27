#!/usr/bin/env node
// Fan the icon masters in src/icons/ out to every compiled node directory.
// n8n resolves `file:` icons relative to the directory holding the .node.js
// file, so a shared master has to be copied next to each node at build time.
// Node declarations are the source of truth: whatever a node asks for must
// exist in src/icons/, or the build fails here rather than shipping a node
// that renders as a blank placeholder in the canvas.
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const iconsDir = join(pkgRoot, 'src', 'icons');
const nodesDir = join(pkgRoot, 'src', 'nodes');

// Matches both `icon: 'file:x.svg'` and `icon: { light: 'file:x.svg', dark: 'file:y.svg' }`.
const ICON_DECLARATION = /icon:\s*(\{[^}]*\}|'[^']*'|"[^"]*")/;
const ICON_FILE = /file:([\w.-]+\.(?:svg|png))/g;

const missing = [];
let copied = 0;

for (const nodeName of readdirSync(nodesDir)) {
  const sourceDir = join(nodesDir, nodeName);
  const nodeFile = join(sourceDir, `${nodeName}.node.ts`);
  if (!existsSync(nodeFile)) continue;

  const declaration = ICON_DECLARATION.exec(readFileSync(nodeFile, 'utf8'));
  if (!declaration) {
    missing.push(`${nodeName} declares no icon`);
    continue;
  }

  const icons = [...declaration[1].matchAll(ICON_FILE)].map((match) => match[1]);
  if (!icons.length) {
    missing.push(`${nodeName} declares an icon with no file: reference`);
    continue;
  }

  const outDir = join(pkgRoot, 'dist', 'nodes', nodeName);
  mkdirSync(outDir, { recursive: true });
  for (const icon of new Set(icons)) {
    const master = join(iconsDir, icon);
    if (!existsSync(master)) {
      missing.push(`${nodeName} wants src/icons/${icon}, which does not exist`);
      continue;
    }
    copyFileSync(master, join(outDir, icon));
    copied += 1;
  }
}

if (missing.length) {
  console.error('icon check failed:');
  for (const problem of missing) console.error(`  - ${problem}`);
  process.exit(1);
}

console.log(`copied ${copied} node icons from src/icons -> dist/nodes/*`);
