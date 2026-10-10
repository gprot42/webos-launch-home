'use strict';

const assert = require('node:assert');
const {test} = require('node:test');

const generated = require('../src/js/aerial-videos.json');

const aerialReady = import('../src/js/aerial.js');
const backgroundsReady = import('../src/js/backgrounds.js');

test('runtime catalog is the generated videos plus the drone fallback', async function () {
  const aerial = await aerialReady;
  const catalog = aerial.getAerialCatalog();
  assert.strictEqual(catalog.length, generated.videos.length + aerial.DRONE_FALLBACK.length);
  assert.ok(catalog.length >= generated.videos.length);
  catalog.forEach(function (video) {
    assert.ok(video.id, 'every clip has an id');
    assert.ok(video.sources, 'every clip has sources');
  });
});

test('lookup by id returns the entry; unknown/empty ids return null', async function () {
  const aerial = await aerialReady;
  const first = generated.videos[0];
  const found = aerial.findAerialVideoById(first.id);
  assert.strictEqual(found, first);
  assert.strictEqual(aerial.findAerialVideoById('does-not-exist'), null);
  assert.strictEqual(aerial.findAerialVideoById(''), null);
  assert.strictEqual(aerial.findAerialVideoById(undefined), null);
});

test('auto climbs the ladder: HDR, then 4K SDR, then 1080p', async function () {
  const {resolveAerialQuality} = await aerialReady;
  assert.strictEqual(resolveAerialQuality({quality: 'auto', canPlayHevc: true, hdrCapable: true}), 'uhdHdr');
  assert.strictEqual(resolveAerialQuality({quality: 'auto', canPlayHevc: true, hdrCapable: false}), 'uhdSdr');
  assert.strictEqual(resolveAerialQuality({quality: 'auto', canPlayHevc: false, hdrCapable: false}), 'hdH264');
  // HDR needs HEVC decode too.
  assert.strictEqual(resolveAerialQuality({quality: 'auto', canPlayHevc: false, hdrCapable: true}), 'hdH264');
  // Unset quality behaves like auto.
  assert.strictEqual(resolveAerialQuality({canPlayHevc: true, hdrCapable: true}), 'uhdHdr');
});

test('explicit modes force their variant and fall through when unsupported', async function () {
  const {resolveAerialQuality} = await aerialReady;
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: true, hdrCapable: true}), 'uhdHdr');
  // No HDR panel → next best (4K SDR when HEVC decodes).
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: true, hdrCapable: false}), 'uhdSdr');
  // No HEVC → 1080p.
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: false, hdrCapable: true}), 'hdH264');
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-sdr', canPlayHevc: true, hdrCapable: true}), 'uhdSdr');
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-sdr', canPlayHevc: false, hdrCapable: true}), 'hdH264');
  assert.strictEqual(resolveAerialQuality({quality: 'hd-h264', canPlayHevc: true, hdrCapable: true}), 'hdH264');
});

test('Performance mode always resolves to 1080p H.264', async function () {
  const {resolveAerialQuality} = await aerialReady;
  assert.strictEqual(resolveAerialQuality({quality: 'auto', perfMode: true, canPlayHevc: true, hdrCapable: true}), 'hdH264');
  assert.strictEqual(resolveAerialQuality({quality: 'uhd-hdr', perfMode: true, canPlayHevc: true, hdrCapable: true}), 'hdH264');
});

test('resolveAerialSourceUrl walks down to the best non-empty URL', async function () {
  const {resolveAerialSourceUrl} = await aerialReady;
  const full = {uhdHdr: 'hdr.mov', uhdSdr: 'sdr.mov', hdH264: 'hd.mov'};
  assert.strictEqual(resolveAerialSourceUrl(full, {quality: 'auto', canPlayHevc: true, hdrCapable: true}), 'hdr.mov');
  // Empty HDR URL falls through to 4K SDR.
  assert.strictEqual(
    resolveAerialSourceUrl({uhdHdr: '', uhdSdr: 'sdr.mov', hdH264: 'hd.mov'},
      {quality: 'auto', canPlayHevc: true, hdrCapable: true}),
    'sdr.mov'
  );
  // Explicit 4K SDR but only 1080p present.
  assert.strictEqual(
    resolveAerialSourceUrl({uhdHdr: 'hdr.mov', uhdSdr: '', hdH264: 'hd.mov'},
      {quality: 'uhd-sdr', canPlayHevc: true, hdrCapable: true}),
    'hd.mov'
  );
  // Perf mode.
  assert.strictEqual(
    resolveAerialSourceUrl(full, {perfMode: true, canPlayHevc: true, hdrCapable: true}),
    'hd.mov'
  );
  assert.strictEqual(resolveAerialSourceUrl({}, {quality: 'auto'}), '');
  assert.strictEqual(resolveAerialSourceUrl(null, {quality: 'auto'}), '');
});

test('normalizeVideoConfig defaults and cleans the video fields', async function () {
  const {normalizeVideoConfig} = await aerialReady;
  assert.deepStrictEqual(normalizeVideoConfig({}), {videoId: '', videoIds: [], videoQuality: 'auto'});
  const kept = normalizeVideoConfig({
    source: 'video',
    videoId: 'A1',
    videoIds: ['A1', '', 7, 'A2'],
    videoQuality: 'uhd-sdr'
  });
  assert.strictEqual(kept.source, 'video');
  assert.strictEqual(kept.videoId, 'A1');
  assert.deepStrictEqual(kept.videoIds, ['A1', 'A2']);
  assert.strictEqual(kept.videoQuality, 'uhd-sdr');
  // An unknown quality falls back to auto.
  assert.strictEqual(normalizeVideoConfig({videoQuality: 'bogus'}).videoQuality, 'auto');
});

test('resolveAerialSelection handles single, subset, all and stale ids', async function () {
  const aerial = await aerialReady;
  const ids = generated.videos.map(function (video) { return video.id; });

  const single = aerial.resolveAerialSelection({videoId: ids[0]});
  assert.deepStrictEqual(single.map(function (v) { return v.id; }), [ids[0]]);

  const subset = aerial.resolveAerialSelection({videoIds: [ids[2], ids[0]]});
  assert.deepStrictEqual(subset.map(function (v) { return v.id; }), [ids[2], ids[0]]);

  const all = aerial.resolveAerialSelection({});
  assert.strictEqual(all.length, aerial.getAerialCatalog().length);

  // A stale saved id is dropped, not fatal; with no valid subset left it falls
  // back to the whole catalog.
  const staleSingle = aerial.resolveAerialSelection({videoId: 'gone'});
  assert.strictEqual(staleSingle.length, aerial.getAerialCatalog().length);
  const mixed = aerial.resolveAerialSelection({videoIds: ['gone', ids[1]]});
  assert.deepStrictEqual(mixed.map(function (v) { return v.id; }), [ids[1]]);
});

test('background config normalization preserves and defaults the video fields', async function () {
  const {normalizeBackgroundConfig, isMediaBackgroundSource} = await backgroundsReady;
  const bg = normalizeBackgroundConfig({
    source: 'video',
    videoId: 'A1',
    videoIds: ['A1', 'A2'],
    videoQuality: 'uhd-hdr'
  });
  assert.strictEqual(bg.source, 'video');
  assert.strictEqual(bg.videoId, 'A1');
  assert.deepStrictEqual(bg.videoIds, ['A1', 'A2']);
  assert.strictEqual(bg.videoQuality, 'uhd-hdr');

  const defaults = normalizeBackgroundConfig({source: 'video'});
  assert.strictEqual(defaults.videoId, '');
  assert.deepStrictEqual(defaults.videoIds, []);
  assert.strictEqual(defaults.videoQuality, 'auto');

  // 'video' is image-like so the Display single/slideshow control applies.
  assert.strictEqual(isMediaBackgroundSource('video'), true);
  assert.strictEqual(isMediaBackgroundSource('builtin'), true);
  assert.strictEqual(isMediaBackgroundSource('preset'), false);
  assert.strictEqual(isMediaBackgroundSource('animated-gradient'), false);
});
