import {expect, test} from 'vitest';

import generated from '../src/js/aerial-videos.json';
import * as aerial from '../src/js/aerial.js';
import {normalizeBackgroundConfig, isMediaBackgroundSource} from '../src/js/backgrounds.js';

test('runtime catalog is the generated videos plus the drone fallback', function () {
  const catalog = aerial.getAerialCatalog();
  expect(catalog.length).toBe(generated.videos.length + aerial.DRONE_FALLBACK.length);
  expect(catalog.length).toBeGreaterThanOrEqual(generated.videos.length);
  catalog.forEach(function (video) {
    expect(video.id, 'every clip has an id').toBeTruthy();
    expect(video.sources, 'every clip has sources').toBeTruthy();
  });
});

test('lookup by id returns the entry; unknown/empty ids return null', function () {
  const first = generated.videos[0];
  const found = aerial.findAerialVideoById(first.id);
  expect(found).toBe(first);
  expect(aerial.findAerialVideoById('does-not-exist')).toBeNull();
  expect(aerial.findAerialVideoById('')).toBeNull();
  expect(aerial.findAerialVideoById(undefined)).toBeNull();
});

test('auto climbs the ladder: HDR, then 4K SDR, then 1080p', function () {
  const {resolveAerialQuality} = aerial;
  expect(resolveAerialQuality({quality: 'auto', canPlayHevc: true, hdrCapable: true})).toBe('uhdHdr');
  expect(resolveAerialQuality({quality: 'auto', canPlayHevc: true, hdrCapable: false})).toBe('uhdSdr');
  expect(resolveAerialQuality({quality: 'auto', canPlayHevc: false, hdrCapable: false})).toBe('hdH264');
  // HDR needs HEVC decode too.
  expect(resolveAerialQuality({quality: 'auto', canPlayHevc: false, hdrCapable: true})).toBe('hdH264');
  // Unset quality behaves like auto.
  expect(resolveAerialQuality({canPlayHevc: true, hdrCapable: true})).toBe('uhdHdr');
});

test('explicit modes force their variant and fall through when unsupported', function () {
  const {resolveAerialQuality} = aerial;
  expect(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: true, hdrCapable: true})).toBe('uhdHdr');
  // No HDR panel → next best (4K SDR when HEVC decodes).
  expect(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: true, hdrCapable: false})).toBe('uhdSdr');
  // No HEVC → 1080p.
  expect(resolveAerialQuality({quality: 'uhd-hdr', canPlayHevc: false, hdrCapable: true})).toBe('hdH264');
  expect(resolveAerialQuality({quality: 'uhd-sdr', canPlayHevc: true, hdrCapable: true})).toBe('uhdSdr');
  expect(resolveAerialQuality({quality: 'uhd-sdr', canPlayHevc: false, hdrCapable: true})).toBe('hdH264');
  expect(resolveAerialQuality({quality: 'hd-h264', canPlayHevc: true, hdrCapable: true})).toBe('hdH264');
});

test('Performance mode always resolves to 1080p H.264', function () {
  const {resolveAerialQuality} = aerial;
  expect(resolveAerialQuality({quality: 'auto', perfMode: true, canPlayHevc: true, hdrCapable: true})).toBe('hdH264');
  expect(resolveAerialQuality({quality: 'uhd-hdr', perfMode: true, canPlayHevc: true, hdrCapable: true})).toBe('hdH264');
});

test('resolveAerialSourceUrl walks down to the best non-empty URL', function () {
  const {resolveAerialSourceUrl} = aerial;
  const full = {uhdHdr: 'hdr.mov', uhdSdr: 'sdr.mov', hdH264: 'hd.mov'};
  expect(resolveAerialSourceUrl(full, {quality: 'auto', canPlayHevc: true, hdrCapable: true})).toBe('hdr.mov');
  // Empty HDR URL falls through to 4K SDR.
  expect(
    resolveAerialSourceUrl({uhdHdr: '', uhdSdr: 'sdr.mov', hdH264: 'hd.mov'},
      {quality: 'auto', canPlayHevc: true, hdrCapable: true})
  ).toBe('sdr.mov');
  // Explicit 4K SDR but only 1080p present.
  expect(
    resolveAerialSourceUrl({uhdHdr: 'hdr.mov', uhdSdr: '', hdH264: 'hd.mov'},
      {quality: 'uhd-sdr', canPlayHevc: true, hdrCapable: true})
  ).toBe('hd.mov');
  // Perf mode.
  expect(
    resolveAerialSourceUrl(full, {perfMode: true, canPlayHevc: true, hdrCapable: true})
  ).toBe('hd.mov');
  expect(resolveAerialSourceUrl({}, {quality: 'auto'})).toBe('');
  expect(resolveAerialSourceUrl(null, {quality: 'auto'})).toBe('');
});

test('normalizeVideoConfig defaults and cleans the video fields', function () {
  const {normalizeVideoConfig} = aerial;
  expect(normalizeVideoConfig({})).toEqual({videoId: '', videoIds: [], videoQuality: 'auto'});
  const kept = normalizeVideoConfig({
    source: 'video',
    videoId: 'A1',
    videoIds: ['A1', '', 7, 'A2'],
    videoQuality: 'uhd-sdr'
  });
  expect(kept.source).toBe('video');
  expect(kept.videoId).toBe('A1');
  expect(kept.videoIds).toEqual(['A1', 'A2']);
  expect(kept.videoQuality).toBe('uhd-sdr');
  // An unknown quality falls back to auto.
  expect(normalizeVideoConfig({videoQuality: 'bogus'}).videoQuality).toBe('auto');
});

test('resolveAerialSelection handles single, subset, all and stale ids', function () {
  const ids = generated.videos.map(function (video) { return video.id; });

  const single = aerial.resolveAerialSelection({videoId: ids[0]});
  expect(single.map(function (v) { return v.id; })).toEqual([ids[0]]);

  const subset = aerial.resolveAerialSelection({videoIds: [ids[2], ids[0]]});
  expect(subset.map(function (v) { return v.id; })).toEqual([ids[2], ids[0]]);

  const all = aerial.resolveAerialSelection({});
  expect(all.length).toBe(aerial.getAerialCatalog().length);

  // A stale saved id is dropped, not fatal; with no valid subset left it falls
  // back to the whole catalog.
  const staleSingle = aerial.resolveAerialSelection({videoId: 'gone'});
  expect(staleSingle.length).toBe(aerial.getAerialCatalog().length);
  const mixed = aerial.resolveAerialSelection({videoIds: ['gone', ids[1]]});
  expect(mixed.map(function (v) { return v.id; })).toEqual([ids[1]]);
});

test('background config normalization preserves and defaults the video fields', function () {
  const bg = normalizeBackgroundConfig({
    source: 'video',
    videoId: 'A1',
    videoIds: ['A1', 'A2'],
    videoQuality: 'uhd-hdr'
  });
  expect(bg.source).toBe('video');
  expect(bg.videoId).toBe('A1');
  expect(bg.videoIds).toEqual(['A1', 'A2']);
  expect(bg.videoQuality).toBe('uhd-hdr');

  const defaults = normalizeBackgroundConfig({source: 'video'});
  expect(defaults.videoId).toBe('');
  expect(defaults.videoIds).toEqual([]);
  expect(defaults.videoQuality).toBe('auto');

  // 'video' is image-like so the Display single/slideshow control applies.
  expect(isMediaBackgroundSource('video')).toBe(true);
  expect(isMediaBackgroundSource('builtin')).toBe(true);
  expect(isMediaBackgroundSource('preset')).toBe(false);
  expect(isMediaBackgroundSource('animated-gradient')).toBe(false);
});
