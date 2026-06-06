#!/usr/bin/env node
/**
 * Pre-compiles JSX in index.html to plain JS.
 * Run: node build.js
 * Output: dist/index.html (no Babel needed in browser)
 */
const fs   = require('fs');
const path = require('path');
const babel = require('@babel/core');

const src  = fs.readFileSync('index.html', 'utf8');

// Extract the babel script
const match = src.match(/(<script type="text\/babel">)([\s\S]*?)(<\/script>)/);
if (!match) { console.error('No babel script found'); process.exit(1); }

const [full, open, jsx, close] = match;

// Compile JSX → JS
let compiled;
try {
  const result = babel.transform(jsx, {
    presets: ['@babel/preset-react'],
    plugins: [],
  });
  compiled = result.code;
  console.log('✅ Babel compile OK — input:', jsx.length, 'chars, output:', compiled.length, 'chars');
} catch(e) {
  console.error('❌ Babel ERROR at line', e.loc?.line, ':', e.message);
  process.exit(1);
}

// Replace <script type="text/babel"> with plain <script>
// Also remove the Babel CDN script tag (no longer needed)
let output = src
  .replace('<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>', '<!-- babel removed: pre-compiled -->')
  .replace(full, '<script>\n' + compiled + '\n</script>');

// Write to dist/
fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync('dist/index.html', output);
console.log('✅ dist/index.html written —', Math.round(output.length/1024), 'KB');
