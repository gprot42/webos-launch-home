'use strict';

const assert = require('node:assert');
const {test} = require('node:test');

const {buildCatalog, verifyCatalog} = require('../scripts/update-aerials');
const feed = require('./fixtures/aerial-entries.json');

function ids(catalog) {
  return catalog.videos.map(function (video) { return video.id; });
}

test('default catalog keeps only shuffle/top-level clips, in preferredOrder', function () {
  const catalog = buildCatalog(feed.assets, {now: '2026-01-01T00:00:00.000Z', source: 'fixture'});
  assert.deepStrictEqual(ids(catalog), ['A002', 'A007', 'A001', 'A008', 'A010']);
  assert.strictEqual(catalog.generatedAt, '2026-01-01T00:00:00.000Z');
  assert.strictEqual(catalog.source, 'fixture');
});

test('keeps clips that omit the shuffle/top-level flags (older feeds)', function () {
  const catalog = buildCatalog(feed.assets, {});
  assert.ok(ids(catalog).includes('A010'));
});

test('dedupes by shotID, keeping the first occurrence', function () {
  const catalog = buildCatalog(feed.assets, {});
  const a001 = catalog.videos.filter(function (video) { return video.id === 'A001'; });
  assert.strictEqual(a001.length, 1);
  assert.strictEqual(a001[0].title, 'Caribbean');
});

test('title falls back to localizedNameKey, then id', function () {
  const catalog = buildCatalog(feed.assets, {});
  const byId = {};
  catalog.videos.forEach(function (video) { byId[video.id] = video.title; });
  assert.strictEqual(byId.A002, 'Hawaii');
  assert.strictEqual(byId.A007, 'A007');
});

test('captures poster and the three quality source URLs', function () {
  const catalog = buildCatalog(feed.assets, {});
  const caribbean = catalog.videos.find(function (video) { return video.id === 'A001'; });
  assert.strictEqual(caribbean.poster, 'https://cdn.example.test/A001_900x580.png');
  assert.deepStrictEqual(caribbean.sources, {
    uhdHdr: 'https://cdn.example.test/A001_HDR_4K_HEVC.mov',
    uhdSdr: 'https://cdn.example.test/A001_SDR_4K_HEVC.mov',
    hdH264: 'https://cdn.example.test/A001_SDR_2K_AVC.mov'
  });
});

test('skips entries with no playable source', function () {
  const catalog = buildCatalog(feed.assets, {});
  assert.ok(!ids(catalog).includes('A009'));
});

test('--all keeps excluded clips; --limit trims after ordering', function () {
  const all = buildCatalog(feed.assets, {all: true});
  assert.deepStrictEqual(ids(all), ['A002', 'A007', 'A001', 'A005', 'A006', 'A008', 'A010']);
  const limited = buildCatalog(feed.assets, {limit: 2});
  assert.deepStrictEqual(ids(limited), ['A002', 'A007']);
});

test('verifyCatalog drops clips with any non-2xx URL', async function () {
  const catalog = buildCatalog(feed.assets, {});
  const head = async function (url) { return !url.includes('A001_900x580'); };
  const result = await verifyCatalog(catalog, {head: head});
  assert.deepStrictEqual(result.dropped, ['A001']);
  assert.deepStrictEqual(ids(result.catalog), ['A002', 'A007', 'A008', 'A010']);
});
