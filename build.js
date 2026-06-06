#!/usr/bin/env node
const fs    = require('fs');
const path  = require('path');
const babel = require('@babel/core');

const src = fs.readFileSync('src/index.html', 'utf8');

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

// Escape </script> inside JS to prevent HTML parser from terminating script early
function safeInline(code) {
  return code.replace(/<\/script>/gi, '<\\/script>');
}

const libs = {
  '<script src="https://unpkg.com/react@18/umd/react.development.js"></script>':
    path.join(__dirname, 'dist/lib/react.min.js'),
  '<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>':
    path.join(__dirname, 'dist/lib/react-dom.min.js'),
  '<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>':
    path.join(__dirname, 'dist/lib/chart.min.js'),
};

let output = src;
for (const [tag, libPath] of Object.entries(libs)) {
  if (!fs.existsSync(libPath)) { console.error('❌ Missing:', libPath); process.exit(1); }
  const libCode = safeInline(fs.readFileSync(libPath, 'utf8'));
  output = output.replace(tag, `<script>\n${libCode}\n</script>`);
  console.log('✅ Inlined', path.basename(libPath), '—', Math.round(libCode.length/1024), 'KB');
}

output = output
  .replace('<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>\n', '')
  .replace(full, `<script>\n${safeInline(compiled)}\n</script>`);

fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync('dist/index.html', output);
console.log(`✅ dist/index.html — ${Math.round(output.length/1024)} KB (self-contained)`);
