// Navbar «Diálogos» panel of containers/web/index.html (docs/dialogos-publicos.md): the button only
// appears when the feed exists, the panel is read-only, and nothing about players is ever rendered.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');

const html = readFileSync(resolve(__dirname, '../../containers/web/index.html'), 'utf8');
const nginx = readFileSync(resolve(__dirname, '../../containers/web/default.conf'), 'utf8');
const sw = readFileSync(resolve(__dirname, '../../containers/web/sw.js'), 'utf8');
const block = (() => {
  const start = html.indexOf('// ---- Panel «Diálogos»');
  const end = html.indexOf('// Enganche heredado del captcha', start);
  assert.ok(start > 0 && end > start, 'the dialog panel script block must exist');
  return html.slice(start, end);
})();

test('the navbar button sits in the dock, starts hidden and controls the panel', () => {
  const button = html.match(/<button id="dialogos"[^>]*>/);
  assert.ok(button, 'index.html must contain #dialogos');
  assert.match(button[0], /\shidden[\s>]/, 'hidden until the feed answers');
  assert.match(button[0], /aria-controls="dlg-panel"/);
  assert.match(button[0], /aria-expanded="false"/);
  const dock = html.match(/<div class="dock">([\s\S]*?)<\/div>\s*<\/nav>/);
  assert.ok(dock && dock[1].includes('id="dialogos"'), 'the button belongs to the navbar dock');
  assert.match(html, /<aside id="dlg-panel"[^>]*\sinert[\s>]/, 'the closed panel is inert');
});

test('the panel only talks to the same-origin feed, without credentials', () => {
  const urls = [...block.matchAll(/fetch\(([^,]+),/g)].map(m => m[1]);
  assert.deepEqual(urls.length, 2, 'feed plus bot-mention index for the Yo tab');
  assert.match(block, /fetch\('\/dialog\/feed\.json'/);
  assert.match(block, /fetch\('\/dialog-bots-mentions\.json'/);
  assert.match(block, /credentials: 'omit'/);
  assert.match(block, /cache: 'no-store'/);
  assert.match(block, /pedirDialogos\(0\)/, 'the page-load probe asks for zero events');
});

test('polling runs every 10 s only while the panel is open', () => {
  assert.match(block, /relojDlg = setInterval\([\s\S]*?, 10000\)/);
  assert.match(block, /function cerrarDialogos\(\) \{[\s\S]*?clearInterval\(relojDlg\)/);
  assert.match(block, /if \(dlgCargando \|\| !dlgAbierto\) return;/);
});

test('rendering never uses HTML parsing and never reads player fields', () => {
  assert.doesNotMatch(block, /innerHTML|insertAdjacentHTML|outerHTML|document\.write/);
  for (const field of ['human_text', 'player_id']) {
    assert.ok(!block.includes(field), `the panel must not read ${field}`);
  }
  assert.doesNotMatch(block, /\be\.player\b|\['player'\]/, 'the panel must not read the player hash');
});

test('the panels and the puffle balloon never share the screen', () => {
  assert.match(html, /function abrirInfra\(\) \{[\s\S]*?cerrarDialogos\(\);/);
  assert.match(block, /function abrirDialogos\(\) \{[\s\S]*?cerrar\(\);\s*cerrarInfra\(\);/);
  assert.match(html, /const alternar = \(\) => \{ if \(globo\.hidden\) \{ cerrarInfra\(\); cerrarDialogos\(\);/);
});

test('nginx proxies exactly one GET path to the loopback sidecar and 404s the rest', () => {
  const loc = nginx.match(/location = \/dialog\/feed\.json \{([\s\S]*?)\n    \}/);
  assert.ok(loc, 'default.conf must route /dialog/feed.json');
  assert.match(loc[1], /limit_except GET \{ deny all; \}/);
  assert.match(loc[1], /proxy_pass http:\/\/127\.0\.0\.1:8767\/feed\$is_args\$args;/);
  assert.match(loc[1], /error_page 502 504 =404/);
  assert.match(loc[1], /add_header Cache-Control 'no-store' always;/);
  assert.match(nginx, /location \/dialog\/ \{ return 404; \}/);
});

test('the service worker never caches the feed', () => {
  assert.match(sw, /url\.pathname\.startsWith\('\/dialog\/'\)[\s\S]{0,80}fetch\(event\.request, \{cache: 'no-store'\}\)/);
});
