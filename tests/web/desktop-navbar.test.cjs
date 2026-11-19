// Desktop auto-hide navbar (containers/web/index.html): only when the game is running.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const html = readFileSync(resolve(__dirname, '../../containers/web/index.html'), 'utf8');

test('chat restrict overlay script exists for builds and glaciar-dev', () => {
  assert.match(html, /motorListo = true/);
  assert.match(html, /pintarBarraRetractil/);
  assert.match(html, /classList\.toggle\('bar-auto'/);
  assert.match(html, /juegoEnMarcha = \(\) => motorListo && vistaLogin === null && avatarEnMundo/);
  assert.match(html, /notificarAvatarEnMundo/);
  assert.match(html, /glaciarcamera/i);
});

test('mobile layouts are not marked bar-revealable', () => {
  assert.match(html, /esEscritorioJuego = \(\) => !mqVertical\.matches && !mqRiel\.matches/);
  assert.match(html, /\.bar-revealable\.bar-auto/);
  assert.match(html, /id="bar-hover-zone"/);
});