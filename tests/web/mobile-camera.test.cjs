// Portrait camera of containers/web/index.html: it follows the local penguin from the stage
// positions that the patched engine reports (experiments/puffle_sim/client/mobile_camera.as).
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const html = readFileSync(resolve(__dirname, '../../containers/web/index.html'), 'utf8');
const inline = html.match(/<script id="camara-glaciar">([\s\S]*?)<\/script>/);
const context = {};
vm.runInNewContext(inline ? inline[1] : '', context);
const Camera = context.GlaciarMobileCamera;

// A 390x666 portrait view of the full-height game; Ruffle draws the stage 1.2 % wider than #game.
const geometry = {viewWidth: 390, gameWidth: 1054.5, renderWidth: 1067.154};
const target = x => Camera.panForPosition(x, geometry);
const onScreen = (x, pan) =>
  geometry.gameWidth / 2 + (x / 760 - .5) * geometry.renderWidth - pan * (geometry.gameWidth - geometry.viewWidth);

function fixture() {
  let active = true, reduced = false, clock = 1000, serial = 0;
  const frames = new Map(), rendered = [];
  const camera = new Camera({
    geometry: () => geometry, render: pan => rendered.push(pan),
    active: () => active, reducedMotion: () => reduced, now: () => clock,
    requestFrame: callback => { frames.set(++serial, callback); return serial; },
    cancelFrame: id => frames.delete(id),
  });
  return {
    camera, frames, rendered,
    setActive: value => { active = value; },
    setReduced: value => { reduced = value; },
    // One engine frame later (30 fps), the engine reports where the avatar is.
    report: (x, y = 300, reset = 0) => { clock += 33; return camera.handleMessage('glaciarCamera', `${x},${y},${reset}`); },
    wait: ms => { clock += ms; },
    settle: () => {
      for (let i = 0, t = clock; i < 200 && frames.size; i++) {
        const pending = [...frames.values()];
        frames.clear();
        t += 16;
        pending.forEach(callback => callback(t));
      }
    },
  };
}

test('the page inlines the camera before the script that builds it', () => {
  assert.ok(inline, 'index.html must contain <script id="camara-glaciar">');
  assert.equal(typeof Camera, 'function');
  assert.ok(html.indexOf('<script id="camara-glaciar">') < html.indexOf('new GlaciarMobileCamera('));
});

test('centers the avatar and clamps at the room edges', () => {
  assert.ok(Math.abs(target(380) - .5) < 1e-9);
  assert.equal(target(0), 0);
  assert.equal(target(760), 1);
  assert.ok(Math.abs(onScreen(520, target(520)) - geometry.viewWidth / 2) < 1e-6);
  assert.equal(Camera.panForPosition(520, {...geometry, viewWidth: 1200}), .5);
  assert.equal(Camera.panForPosition(520, {...geometry, gameWidth: 0}), null);
  assert.equal(Camera.panForPosition(NaN, geometry), null);
});

test('locks onto the penguin while it walks: no lag and no animation frames', () => {
  const f = fixture();
  assert.equal(f.report(380, 300, 1), true);
  assert.equal(f.camera.pan, target(380));
  for (let x = 384; x <= 520; x += 4) {
    f.report(x);
    assert.ok(Math.abs(onScreen(x, f.camera.pan) - geometry.viewWidth / 2) < 1e-6);
  }
  assert.equal(f.frames.size, 0);
});

test('looking aside pauses following until the next walk, which glides back and locks again', () => {
  const f = fixture();
  f.report(380, 300, 1);
  f.report(384);
  f.camera.panManually(.1);
  assert.equal(f.camera.following, false);
  f.report(388);
  f.report(392);
  assert.equal(f.camera.pan, .1, 'the rest of the same walk does not steal the view');
  assert.equal(f.frames.size, 0);
  f.wait(1000);
  f.report(396);
  assert.equal(f.camera.following, true);
  assert.equal(f.frames.size, 1, 'glides instead of jumping');
  f.settle();
  assert.equal(f.camera.attached, true);
  assert.equal(f.camera.pan, target(396));
  f.report(400);
  assert.equal(f.camera.pan, target(400));
  assert.equal(f.frames.size, 0);
});

test('the back-to-my-penguin button glides back right away', () => {
  const f = fixture();
  f.report(380, 300, 1);
  f.camera.panManually(.9);
  f.camera.setFollowing(true);
  assert.equal(f.frames.size, 1);
  f.settle();
  assert.equal(f.camera.pan, target(380));
  assert.equal(f.camera.attached, true);
});

test('a new room cuts to the penguin and resumes following', () => {
  const f = fixture();
  f.report(380, 300, 1);
  f.camera.panManually(0);
  f.report(120, 280, 1);
  assert.equal(f.camera.following, true);
  assert.equal(f.camera.pan, target(120));
  assert.equal(f.frames.size, 0);
});

test('loading screens and mini-games are centered; the next room cuts back to the penguin', () => {
  const f = fixture();
  f.report(600, 300, 1);
  assert.ok(f.camera.pan > .8);
  assert.equal(f.camera.handleMessage('glaciarCamera', 'clear'), true);
  assert.equal(f.camera.position, null);
  assert.equal(f.camera.pan, .5, 'cuts to the middle with the scene change, so the animation starts centered');
  assert.equal(f.frames.size, 0);
  f.report(200);
  assert.equal(f.camera.pan, target(200), 'a first sighting is a cut even without the reset flag');
  assert.equal(f.frames.size, 0);
});

test('a loading screen is centered even after looking aside, and the player can still look around', () => {
  const f = fixture();
  f.report(380, 300, 1);
  f.camera.panManually(0);
  f.camera.handleMessage('glaciarCamera', 'clear');
  assert.equal(f.camera.pan, .5);
  f.camera.panManually(.2);
  f.camera.refresh(true);
  assert.equal(f.camera.pan, .2, 'a resize does not undo a manual look at a wide mini-game');
  f.report(300, 300, 1);
  assert.equal(f.camera.following, true);
  assert.equal(f.camera.pan, target(300));
});

test('before the first sighting (login, server list) the view stays centered unless moved', () => {
  const f = fixture();
  f.camera.refresh(true);
  assert.equal(f.camera.pan, .5);
  assert.deepEqual(f.rendered, []);
  f.camera.panManually(.8);
  f.camera.refresh(true);
  assert.equal(f.camera.pan, .8);
});

test('whole-game, landscape and hidden views never move; reduced motion never glides', () => {
  const f = fixture();
  f.setActive(false);
  f.report(600, 300, 1);
  f.report(604);
  assert.deepEqual(f.rendered, []);
  f.setActive(true);
  f.camera.refresh(true);
  assert.equal(f.camera.pan, target(604));
  f.setReduced(true);
  f.camera.panManually(0);
  f.camera.setFollowing(true);
  assert.equal(f.frames.size, 0);
  assert.equal(f.camera.pan, target(604));
  f.camera.handleMessage('glaciarCamera', 'clear');
  assert.equal(f.camera.pan, .5);
  assert.equal(f.frames.size, 0);
  f.setActive(false);
  f.report(100, 300, 1);
  assert.equal(f.camera.pan, .5, 'inactive views ignore new rooms too');
});

test('the visible stage strip matches the screen edges, so the engine can center the chat in it', () => {
  for (const x of [120, 380, 520, 700]) {
    const pan = target(x);
    const strip = Camera.visibleStage(pan, geometry);
    assert.ok(Math.abs(onScreen(strip.left, pan)) < 1e-6, 'left edge of the screen');
    assert.ok(Math.abs(onScreen(strip.right, pan) - geometry.viewWidth) < 1e-6, 'right edge of the screen');
    assert.ok(Math.abs(strip.right - strip.left - geometry.viewWidth / geometry.renderWidth * 760) < 1e-6);
  }
  // Not clamped at a room edge, the strip is centered on the penguin.
  const strip = Camera.visibleStage(target(520), geometry);
  assert.ok(Math.abs((strip.left + strip.right) / 2 - 520) < 1e-6);
  assert.equal(Camera.visibleStage(.5, {...geometry, viewWidth: 1200}), null, 'the whole game fits');
  assert.equal(Camera.visibleStage(NaN, geometry), null);
  assert.equal(Camera.visibleStage(.5, {...geometry, renderWidth: 0}), null);
});

test('the engine gets "left,right" while the view is cropped and "" when the whole game shows', () => {
  const f = fixture();
  f.report(520, 300, 1);
  const [left, right] = f.camera.hudView().split(',').map(Number);
  const strip = Camera.visibleStage(f.camera.pan, geometry);
  assert.ok(Math.abs(left - strip.left) < .01 && Math.abs(right - strip.right) < .01);
  f.camera.panManually(0);
  assert.ok(Number(f.camera.hudView().split(',')[0]) < left, 'follows a manual look aside too');
  f.setActive(false);
  assert.equal(f.camera.hudView(), '');
});

test('the page exposes the strip to the engine as a global ExternalInterface function', () => {
  assert.match(html, /window\.glaciarHudView = \(\) => camara\.hudView\(\);/);
  assert.ok(html.indexOf('window.glaciarHudView') < html.indexOf('player.ruffle().load('));
});

test('ignores unrelated commands and malformed reports; the command matches in any case', () => {
  const f = fixture();
  for (const [command, payload] of [
    ['other', '380,300,1'], [undefined, '380,300,1'], ['glaciarCamera', null], ['glaciarCamera', ''],
    ['glaciarCamera', ',300,0'], ['glaciarCamera', 'NaN,300,0'], ['glaciarCamera', 'Infinity,300,0'],
    ['glaciarCamera', '380,300,2'], ['glaciarCamera', '380,300,0,extra'],
  ]) assert.equal(f.camera.handleMessage(command, payload), false);
  assert.equal(f.camera.position, null);
  assert.deepEqual(f.rendered, []);
  assert.equal(f.camera.handleMessage('GLACIARCAMERA', '380,300,1'), true);
});
