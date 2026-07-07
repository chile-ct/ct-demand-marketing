#!/usr/bin/env node
/**
 * Build: compile JSX → plain JS. Use CDN for React/Chart.js (pinned, reliable).
 * Output: dist/index.html — works on localhost AND GitHub Pages.
 */
const fs   = require('fs');
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

let output = src
  // Replace dev React with prod (faster, smaller)
  .replace(
    'https://unpkg.com/react@18/umd/react.development.js',
    'https://unpkg.com/react@18.2.0/umd/react.production.min.js'
  )
  .replace(
    'https://unpkg.com/react-dom@18/umd/react-dom.development.js',
    'https://unpkg.com/react-dom@18.2.0/umd/react-dom.production.min.js'
  )
  // Pin Chart.js version
  .replace(
    'https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js',
    'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'
  )
  // Remove Babel CDN (not needed anymore)
  .replace(/<script src="https:\/\/unpkg\.com\/@babel\/standalone[^"]*"><\/script>\n?/, '')
  // Replace babel script with compiled plain JS
  .replace(full, '<script>\n' + compiled + '\n</script>');

// Inject cache-busting: auto-redirect to ?v=<ts> when a new build is detected
const buildTs = Date.now();
const cacheBuster = `<script>
(function(){
  var ts='${buildTs}';
  var k='_dashboard_v';
  if(localStorage.getItem(k)!==ts){
    localStorage.setItem(k,ts);
    if(location.search.indexOf('_v=')<0)
      location.replace(location.pathname+'?_v='+ts);
  }
})();
</script>`;
output = output.replace('</head>', cacheBuster + '\n</head>');

fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync('dist/index.html', output);
fs.writeFileSync('index.html', output);
console.log('✅ dist/index.html + index.html written —', Math.round(output.length/1024), 'KB');
