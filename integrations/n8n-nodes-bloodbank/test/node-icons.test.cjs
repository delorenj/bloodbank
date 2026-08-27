const assert = require('node:assert/strict');
const test = require('node:test');
const { existsSync, readFileSync } = require('node:fs');
const { join } = require('node:path');

const { Bloodbank, BloodbankTrigger, PlaneBloodbank } = require('../src/index.ts');

const iconsDir = join(__dirname, '..', 'src', 'icons');
const nodes = [
  ['Bloodbank', new Bloodbank(), 'bloodbank'],
  ['BloodbankTrigger', new BloodbankTrigger(), 'bloodbank'],
  ['PlaneBloodbank', new PlaneBloodbank(), 'planeBloodbank'],
];

test('every node ships a themed icon pair drawn from the brand mark', () => {
  for (const [name, node, stem] of nodes) {
    const icon = node.description.icon;
    assert.deepEqual(
      icon,
      { light: `file:${stem}.svg`, dark: `file:${stem}.dark.svg` },
      `${name} must declare a light/dark icon pair`,
    );
    for (const reference of Object.values(icon)) {
      const file = reference.replace(/^file:/, '');
      assert.ok(existsSync(join(iconsDir, file)), `${name} references missing src/icons/${file}`);
    }
  }
});

test('icon masters are self-contained, themeable SVGs', () => {
  const files = new Set(
    nodes.flatMap(([, node]) =>
      Object.values(node.description.icon).map((reference) => reference.replace(/^file:/, '')),
    ),
  );
  for (const file of files) {
    const svg = readFileSync(join(iconsDir, file), 'utf8');
    assert.match(svg, /viewBox="0 0 64 64"/, `${file} must use the shared 64x64 viewBox`);
    assert.doesNotMatch(svg, /<image\b/, `${file} must not embed a raster image`);
    assert.doesNotMatch(
      svg,
      /(?:href|src)\s*=\s*"https?:/,
      `${file} must not reference a remote resource`,
    );
    // The mark is a blood drop; a dark-canvas variant must not paint it in ink.
    const strokes = svg.match(/stroke="#[0-9A-Fa-f]{6}"/g) || [];
    assert.ok(strokes.length > 0, `${file} must stroke the orbit and pulse marks`);
    if (file.endsWith('.dark.svg')) {
      assert.ok(
        strokes.every((stroke) => stroke.toUpperCase().includes('FAF8F7')),
        `${file} must stroke in the off-white brand ink for dark canvases`,
      );
    }
  }
});
