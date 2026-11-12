// Phone login of containers/web/index.html: the patched login.swf (experiments/puffle_sim/client/
// mobile_login.as) skips the start banner and reports which login view is showing; the page scales
// and places the whole stage so that view fits the screen.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const html = readFileSync(resolve(__dirname, '../../containers/web/index.html'), 'utf8');
const inline = html.match(/<script id="camara-glaciar">([\s\S]*?)<\/script>/);
const context = {};
vm.runInNewContext(inline ? inline[1] : '', context);
const Frame = context.GlaciarLoginFrame;
const STAGE = [0, 0, 760, 480];

// <main> at common phone sizes: portrait (no slider while the login is framed) and the landscape rail.
const PHONES = {
  'iPhone SE': {viewWidth: 375, viewHeight: 603},
  'Android 360': {viewWidth: 360, viewHeight: 676},
  'iPhone 15': {viewWidth: 390, viewHeight: 780},
  'iPhone Pro Max': {viewWidth: 430, viewHeight: 868},
  'landscape SE': {viewWidth: 603, viewHeight: 375},
  'landscape 15': {viewWidth: 780, viewHeight: 390},
};
const plain = value => JSON.parse(JSON.stringify(value));

function fitsOnScreen(rect, view, label) {
  const layout = Frame.layout(rect, view);
  assert.ok(layout, `${label}: no layout`);
  const [left, top] = Frame.toScreen([rect[0], rect[1]], layout);
  const [right, bottom] = Frame.toScreen([rect[2], rect[3]], layout);
  const edge = 11.99;  // MARGEN minus rounding
  assert.ok(left >= edge && top >= edge, `${label}: ${left.toFixed(1)},${top.toFixed(1)} is off the top-left`);
  assert.ok(right <= view.viewWidth - edge && bottom <= view.viewHeight - edge,
            `${label}: ${right.toFixed(1)},${bottom.toFixed(1)} is off the bottom-right`);
  return layout;
}

test('the page inlines the login framing next to the camera', () => {
  assert.ok(inline, 'index.html must contain <script id="camara-glaciar">');
  assert.equal(typeof Frame, 'function');
});

test('reads the views the patched login reports, per language', () => {
  const es = Frame.parse('newplayer', 'es'), en = Frame.parse('newplayer', 'en');
  assert.equal(es.view, 'newplayer');
  assert.equal(es.overlay, null);
  assert.ok(en.rect[0] > es.rect[0], 'English labels are shorter: the form starts further right');
  assert.ok(Frame.parse('newplayer-noback', 'es').rect[3] < es.rect[3], 'without "Back" the form ends higher');
  assert.deepEqual(plain(Frame.parse('playerlogin', 'en').rect), plain(Frame.parse('playerlogin', 'es').rect));
  assert.deepEqual(plain(Frame.parse('newplayer', 'pt').rect), plain(es.rect), 'unknown language falls back to Spanish');
});

test('anything not measured is framed whole, never cut', () => {
  assert.deepEqual(plain(Frame.parse('moreservers').rect), STAGE);
  assert.deepEqual(plain(Frame.parse('startscreen').rect), STAGE);
  assert.deepEqual(plain(Frame.parse('newplayer,dialog,unknownprompt').rect), STAGE);
  assert.deepEqual(plain(Frame.parse('newplayer,error,10,10,10,10').rect), STAGE);
});

test('the saved-penguin cards use the measured cards plus the link below them', () => {
  const {rect, overlay} = Frame.parse('playerselection,box,258,38,502,370');
  assert.equal(overlay, null);
  assert.equal(rect[0], 258);
  assert.equal(rect[2], 502);
  assert.equal(rect[1], 38);
  assert.ok(rect[3] >= 445, '"Login as a different penguin" stays in the frame');
  assert.ok(Frame.parse('playerselection').rect[3] >= 445, 'without a measure, one card and the link');
});

test('dialogs: the error window by its size, the remember-password prompts by name', () => {
  const error = Frame.parse('newplayer,error,203,140,557,340');
  assert.equal(error.overlay, 'error');
  assert.deepEqual(plain(error.rect), [203, 140, 557, 340]);
  assert.deepEqual(plain(Frame.parse('newplayer,error,-20,-5,900,500').rect), STAGE, 'clamped to the stage');
  for (const name of ['publiccomputer', 'savepassword']) {
    const dialog = Frame.parse(`newplayer,dialog,${name}`);
    assert.equal(dialog.overlay, name);
    assert.ok(dialog.rect[2] - dialog.rect[0] < 760, `${name} frames the panel, not the dimmed stage`);
  }
});

test('the end of the login and malformed reports', () => {
  for (const payload of ['', undefined, null, 42]) assert.equal(Frame.parse(payload), null);
  for (const payload of ['NewPlayer', 'new player', 'newplayer,box', 'newplayer,box,1,2,3', 'newplayer,prompt,1,2,3,4',
                         'newplayer,dialog,Bad Name', 'newplayer,error,a,b,c,d,e']) {
    assert.equal(Frame.parse(payload), null, payload);
  }
});

test('every login view and dialog fits whole on every phone, in both languages', () => {
  const payloads = ['newplayer', 'newplayer-noback', 'playerlogin', 'playerselection', 'worldselection',
                    'playerselection,box,258,38,502,370', 'newplayer,error,203,140,557,340',
                    'newplayer,dialog,publiccomputer', 'newplayer,dialog,savepassword'];
  for (const [phone, view] of Object.entries(PHONES)) {
    for (const lang of ['es', 'en']) {
      for (const payload of payloads) fitsOnScreen(Frame.parse(payload, lang).rect, view, `${phone} ${lang} ${payload}`);
    }
  }
});

test('the form is drawn at about its original size, far larger than the whole stage would be', () => {
  for (const [phone, view] of Object.entries(PHONES)) {
    for (const lang of ['es', 'en']) {
      const {scale} = Frame.layout(Frame.parse('newplayer-noback', lang).rect, view);
      const whole = Math.min(view.viewWidth / 760, view.viewHeight / 480);
      assert.ok(scale >= .99, `${phone} ${lang}: form drawn at ${scale.toFixed(2)}x`);
      assert.ok(scale > 1.5 * whole || view.viewWidth > view.viewHeight, `${phone} ${lang}: no better than the whole stage`);
    }
  }
});

test('the stage covers the screen where it is big enough; otherwise it is centered', () => {
  // Landscape: taller than the screen, so no empty bands above or below.
  const wide = Frame.layout(Frame.parse('newplayer-noback').rect, PHONES['landscape 15']);
  const [, top] = Frame.toScreen([0, 0], wide), [, bottom] = Frame.toScreen([0, 480], wide);
  assert.ok(top <= 0 && bottom >= PHONES['landscape 15'].viewHeight);
  // Portrait: the stage is shorter than the screen; same blue band above and below.
  const tall = Frame.layout(Frame.parse('newplayer-noback').rect, PHONES['iPhone 15']);
  const [, t] = Frame.toScreen([0, 0], tall), [, b] = Frame.toScreen([0, 480], tall);
  assert.ok(Math.abs(t - (PHONES['iPhone 15'].viewHeight - b)) < 1e-6);
  // The left and right edges of the stage never come into view while the form is wider than the screen allows.
  const [left] = Frame.toScreen([0, 0], tall), [right] = Frame.toScreen([760, 0], tall);
  assert.ok(left <= 0 && right >= PHONES['iPhone 15'].viewWidth);
});

test('tablets in portrait stop at twice the original size', () => {
  assert.ok(Frame.layout(Frame.parse('newplayer').rect, {viewWidth: 820, viewHeight: 1100}).scale <= 2);
  assert.equal(Frame.layout(STAGE, {viewWidth: 20, viewHeight: 400}), null, 'no room for the margin');
  assert.equal(Frame.layout('newplayer', PHONES['iPhone 15']), null);
});

test('the page wires the patched login: banner skip on phones, framing, hash-pinned login.swf', () => {
  assert.match(html, /window\.glaciarSkipStart = \(\) => esTelefono\(\);/);
  assert.match(html, /const esTelefono = \(\) => mqVertical\.matches \|\| mqRiel\.matches;/);
  assert.match(html, /command\.toLowerCase\(\) === 'glaciarlogin'/);
  assert.match(html, /GlaciarLoginFrame\.parse\(payload \?\? '', idioma\)/);
  assert.match(html, /\/\^\(\?:\.\*\\\/\)\?login\\\.swf/);
  assert.match(html, /'\/media\/play\/v2\/client\/login\.swf\?v=' \+ encodeURIComponent\(build\['login\.swf'\]\)/);
  assert.match(html, /\/\^\(\?:\.\*\\\/\)\?create\\\.swf/);
  assert.match(html, /'\/media\/play\/v2\/client\/create\.swf\?v=' \+ encodeURIComponent\(build\['create\.swf'\]\)/);
  // While framed, the camera does not move the stage and the whole-game toggle is off.
  assert.match(html, /active: \(\) => mqVertical\.matches && !encuadreCompleto && vistaLogin === null/);
  assert.match(html, /zoomBoton\.disabled = activo;/);
});
