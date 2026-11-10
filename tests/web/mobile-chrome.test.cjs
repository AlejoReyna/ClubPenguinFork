// Phone chrome of containers/web/index.html: the status-bar colour and the ES/EN switch that
// replaces the magnifier in the portrait bar.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');

const html = readFileSync(resolve(__dirname, '../../containers/web/index.html'), 'utf8');
const BLUE = '#016bb6';

test('the status bar zone uses one blue, in Safari (theme-color) and when installed (safe-area strip)', () => {
  assert.match(html, new RegExp(`<meta name="theme-color" content="${BLUE}">`));
  const strip = html.match(/body::before\{([^}]*)\}/);
  assert.ok(strip, 'body::before must paint the safe-area strip');
  assert.match(strip[1], /position:fixed/);
  assert.match(strip[1], /height:env\(safe-area-inset-top\)/);
  assert.match(strip[1], new RegExp(`background:${BLUE}`));
  assert.match(strip[1], /pointer-events:none/);
});

test('the magnifier is gone from the bar and an ES/EN switch takes its place', () => {
  assert.match(html, /<button id="zoom"[^>]*\shidden[\s>]/, 'zoom stays in the DOM (framing code reads it) but hidden');
  assert.doesNotMatch(html, /\.bar #zoom\{order:5;display:inline-flex\}/, 'portrait must not show the magnifier again');
  const sw = html.match(/<button id="idioma-switch"[^>]*>([\s\S]*?)<\/button>/);
  assert.ok(sw, 'index.html must contain #idioma-switch');
  assert.match(sw[0], /role="switch"/);
  assert.match(sw[0], /aria-checked="(true|false)"/);
  assert.match(sw[1], /ES[\s\S]*EN/);
  // Same slot as the magnifier: inside .bar-actions, right after #zoom.
  assert.match(html, /id="zoom"[\s\S]*?<\/button>\s*<button id="idioma-switch"/);
});

test('the switch only exists in portrait, where it replaces the floating language select', () => {
  assert.match(html, /\.bar #zoom,\.bar \.lang-switch\{display:none\}/, 'hidden by default (desktop, landscape rail)');
  const portrait = html.match(/@media\(orientation:portrait\) and \(max-width:900px\)\{([\s\S]*?)\n {2}\}\n/);
  assert.ok(portrait, 'portrait media block not found');
  assert.match(portrait[1], /\.bar \.lang-switch\{display:inline-flex;order:5/);
  assert.match(portrait[1], /\.bar label\{display:none\}/);
});

test('clicking the switch goes through the language select (same storage and reload)', () => {
  assert.match(html, /selector\.value = enIngles \? 'es' : 'en';\s*selector\.dispatchEvent\(new Event\('change'\)\);/);
  assert.match(html, /interruptorIdioma\.setAttribute\('aria-checked', String\(enIngles\)\)/);
});
