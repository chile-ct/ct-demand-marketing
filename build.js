#!/usr/bin/env node
const fs   = require('fs');
const babel = require('@babel/core');

const src  = fs.readFileSync('src/index.html', 'utf8');
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

// Replace CDN scripts with local + replace babel script with compiled
let output = src
  .replace('<script src="https://unpkg.com/react@18/umd/react.development.js"></script>', '<script src="lib/react.min.js"></script>')
  .replace('<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>', '<script src="lib/react-dom.min.js"></script>')
  .replace('<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>', '<script src="lib/chart.min.js"></script>')
  .replace('<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>', '')
  .replace(full, '<script>\n' + compiled + '\n</script>');

fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync('dist/index.html', output);
console.log('✅ dist/index.html written —', Math.round(output.length/1024), 'KB');
