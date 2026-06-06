#!/usr/bin/env node
/**
 * Build: compile JSX + inline all dependencies into one self-contained HTML.
 * Result: dist/index.html works as file://, localhost, GitHub Pages — no server setup needed.
 */
const fs    = require('fs');
const path  = require('path');
const babel = require('@babel/core');

const src = fs.readFileSync('src/index.html', 'utf8');

// Find and compile the JSX script
const match = src.match(/(<script type="text\/babel">)([\s\S]*?)(<\/script>)/);
if (!match) { console.error('No babel script found'); process.exit(1); }
const [full, , jsx] = match;

let compiled;
try {
  compiled = babel.transform(jsx, { presets: ['@babel/preset-react'] }).code;
  console.log('✅ Babel OK — input:', jsx.length, 'output:', compiled.length);
} catch(e) {
  console.error('❌ Babel ERROR line', e.loc?.line, ':', e.message);
  process.exit(1);
}

// Inline each CDN script
const libs = {
  '<script src="https://unpkg.com/react@18/umd/react.development.js"></script>':
    path.join(__dirname, 'dist/lib/react.min.js'),
  '<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>':
    path.join(__dirname, 'dist/lib/react-dom.min.js'),
  '<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>':
    path.join(__dirname, 'dist/lib/chart.min.js'),
};

let output = src;

// Replace CDN scripts with inline versions
for (const [tag, libPath] of Object.entries(libs)) {
  if (fs.existsSync(libPath)) {
    const libCode = fs.readFileSync(libPath, 'utf8');
    output = output.replace(tag, `<script>\n${libCode}\n</script>`);
    console.log('✅ Inlined', path.basename(libPath), '—', Math.round(libCode.length/1024), 'KB');
  } else {
    console.error('❌ Missing lib:', libPath);
    process.exit(1);
  }
}

// Remove Babel CDN (no longer needed)
output = output.replace('<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>\n', '');

// Replace babel script with compiled
output = output.replace(full, '<script>\n' + compiled + '\n</script>');

// Write output
fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync('dist/index.html', output);
const kb = Math.round(output.length / 1024);
console.log(`✅ dist/index.html — ${kb} KB (fully self-contained, no server needed)`);
