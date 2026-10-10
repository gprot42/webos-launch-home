import {expect, test} from 'vitest';

import {buildCatalog, verifyCatalog} from '../scripts/update-aerials.js';
import feed from './fixtures/aerial-entries.json';

function ids(catalog) {
  return catalog.videos.map(function (video) { return video.id; });
}

test('default catalog keeps only shuffle/top-level clips, in preferredOrder', function () {
  const catalog = buildCatalog(feed.assets, {now: '2026-01-01T00:00:00.000Z', source: 'fixture'});
  expect(ids(catalog)).toEqual(['A002', 'A007', 'A001', 'A008', 'A010']);
  expect(catalog.generatedAt).toBe('2026-01-01T00:00:00.000Z');
  expect(catalog.source).toBe('fixture');
});

test('keeps clips that omit the shuffle/top-level flags (older feeds)', function () {
  const catalog = buildCatalog(feed.assets, {});
  expect(ids(catalog)).toContain('A010');
});

test('dedupes by shotID, keeping the first occurrence', function () {
  const catalog = buildCatalog(feed.assets, {});
  const a001 = catalog.videos.filter(function (video) { return video.id === 'A001'; });
  expect(a001.length).toBe(1);
  expect(a001[0].title).toBe('Caribbean');
});

test('title falls back to localizedNameKey, then id', function () {
  const catalog = buildCatalog(feed.assets, {});
  const byId = {};
  catalog.videos.forEach(function (video) { byId[video.id] = video.title; });
  expect(byId.A002).toBe('Hawaii');
  expect(byId.A007).toBe('A007');
});

test('captures poster and the three quality source URLs', function () {
  const catalog = buildCatalog(feed.assets, {});
  const caribbean = catalog.videos.find(function (video) { return video.id === 'A001'; });
  expect(caribbean.poster).toBe('https://cdn.example.test/A001_900x580.png');
  expect(caribbean.sources).toEqual({
    uhdHdr: 'https://cdn.example.test/A001_HDR_4K_HEVC.mov',
    uhdSdr: 'https://cdn.example.test/A001_SDR_4K_HEVC.mov',
    hdH264: 'https://cdn.example.test/A001_SDR_2K_AVC.mov'
  });
});

test('skips entries with no playable source', function () {
  const catalog = buildCatalog(feed.assets, {});
  expect(ids(catalog)).not.toContain('A009');
});

test('--all keeps excluded clips; --limit trims after ordering', function () {
  const all = buildCatalog(feed.assets, {all: true});
  expect(ids(all)).toEqual(['A002', 'A007', 'A001', 'A005', 'A006', 'A008', 'A010']);
  const limited = buildCatalog(feed.assets, {limit: 2});
  expect(ids(limited)).toEqual(['A002', 'A007']);
});

test('verifyCatalog drops clips with any non-2xx URL', async function () {
  const catalog = buildCatalog(feed.assets, {});
  const head = async function (url) { return !url.includes('A001_900x580'); };
  const result = await verifyCatalog(catalog, {head: head});
  expect(result.dropped).toEqual(['A001']);
  expect(ids(result.catalog)).toEqual(['A002', 'A007', 'A008', 'A010']);
});
