#!/usr/bin/env node

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const {execFileSync} = require('child_process');

// Regenerates the aerial-video catalog that backgrounds.js bundles. Maintainer
// tool: it downloads Apple's aerial feed (a .tar, or a raw entries.json), keeps
// the curated clips, and writes src/js/aerial-videos.json. No video bytes are
// ever downloaded — only metadata (titles, posters, stream URLs).
//
//   npm run update:aerials                    # refresh from the default feed
//   npm run update:aerials -- --all           # include non-shuffle / off-top clips
//   npm run update:aerials -- --limit 40      # cap the catalog size
//   npm run update:aerials -- --verify        # HEAD-check URLs, drop dead entries
//   npm run update:aerials -- --source <url|path>
//   npm run update:aerials -- --out <path>    # write somewhere other than src/js/
//   npm run update:aerials -- --insecure      # skip TLS verification (see below)
//
// The default feed is Apple's tvOS 26 aerials. Its URL contains an itunes-assets
// GUID that Apple rotates from time to time, so a 404 here just means "point
// --source at a fresh feed": https://gist.github.com/theothernt/57a51cade0c12c407f48a5121e0939d5
//
// This feed is the default rather than the older `resources-16.tar` because only
// the newer feed carries the includeInShuffle / showInTopLevel / preferredOrder /
// previewImage fields the catalog needs (the tvOS 16 feed has none of them).
const DEFAULT_SOURCE =
  'https://sylvan.apple.com/itunes-assets/Aerials126/v4/c0/45/d9/c045d9d0-9606-1535-62fe-189edb4f79eb/resources-atv-23J-2.tar';

const DEFAULT_OUT = path.join(__dirname, '..', 'src', 'js', 'aerial-videos.json');

// The quality ladder the TV runtime falls back through.
const SOURCE_FIELDS = {
  uhdHdr: 'url-4K-HDR',
  uhdSdr: 'url-4K-SDR',
  hdH264: 'url-1080-H264'
};

function text(value) {
  return String(value == null ? '' : value).trim();
}

function entryId(entry) {
  return text(entry && (entry.shotID || entry.id));
}

function entryTitle(entry, id) {
  return text(entry && (entry.accessibilityLabel || entry.localizedNameKey)) || id;
}

function entryPoster(entry) {
  return text(entry && (entry.previewImage || entry['previewImage-900x580']));
}

function entrySources(entry) {
  const sources = {};
  Object.keys(SOURCE_FIELDS).forEach(function (key) {
    sources[key] = text(entry && entry[SOURCE_FIELDS[key]]);
  });
  return sources;
}

function hasAnySource(sources) {
  return Object.keys(SOURCE_FIELDS).some(function (key) { return !!sources[key]; });
}

// Pure transform: raw Apple entries -> committed catalog. `now` and `source`
// are injectable so tests are deterministic. Keeps only shuffle/top-level clips
// unless `all`, dedupes by id, orders by preferredOrder, then applies `limit`.
function buildCatalog(entries, opts) {
  const options = opts || {};
  const all = !!options.all;
  const limit = options.limit > 0 ? Math.floor(options.limit) : 0;
  const source = options.source || '';
  const generatedAt = options.now || new Date().toISOString();

  const list = Array.isArray(entries) ? entries : (entries && entries.assets) || [];
  const seen = Object.create(null);
  const picked = [];

  list.forEach(function (entry, index) {
    if (!entry || typeof entry !== 'object') return;
    if (!all && (entry.includeInShuffle === false || entry.showInTopLevel === false)) return;
    const id = entryId(entry);
    if (!id || seen[id]) return;
    const sources = entrySources(entry);
    if (!hasAnySource(sources)) return;
    seen[id] = true;
    picked.push({
      id: id,
      title: entryTitle(entry, id),
      poster: entryPoster(entry),
      sources: sources,
      order: typeof entry.preferredOrder === 'number' ? entry.preferredOrder : Infinity,
      index: index
    });
  });

  picked.sort(function (a, b) {
    if (a.order !== b.order) return a.order - b.order;
    return a.index - b.index;
  });

  const videos = (limit ? picked.slice(0, limit) : picked).map(function (video) {
    return {id: video.id, title: video.title, poster: video.poster, sources: video.sources};
  });

  return {generatedAt: generatedAt, source: source, videos: videos};
}

function headRequest(url, opts) {
  const options = opts || {};
  return new Promise(function (resolve) {
    let settled = false;
    function done(ok) {
      if (settled) return;
      settled = true;
      resolve(ok);
    }
    const req = https.request(url, {
      method: 'HEAD',
      rejectUnauthorized: !options.insecure,
      timeout: options.timeoutMs || 15000
    }, function (res) {
      res.resume();
      done(res.statusCode >= 200 && res.statusCode < 400);
    });
    req.on('error', function () { done(false); });
    req.on('timeout', function () { req.destroy(); done(false); });
    req.end();
  });
}

// HEAD-checks every URL of every clip and drops clips with any non-2xx/3xx URL.
// `head` and `headOptions` are injectable for tests.
async function verifyCatalog(catalog, opts) {
  const options = opts || {};
  const head = options.head || headRequest;
  const headOptions = options.headOptions || {};
  const concurrency = options.concurrency || 8;
  const videos = catalog.videos || [];
  const kept = Object.create(null);
  const dropped = [];

  let cursor = 0;
  async function worker() {
    while (cursor < videos.length) {
      const video = videos[cursor];
      cursor += 1;
      const urls = [video.poster].concat(
        Object.keys(SOURCE_FIELDS).map(function (key) { return video.sources[key]; })
      ).filter(Boolean);
      const results = await Promise.all(urls.map(function (url) {
        return head(url, headOptions);
      }));
      if (results.every(Boolean)) kept[video.id] = true;
      else dropped.push(video.id);
    }
  }
  await Promise.all(new Array(concurrency).fill(0).map(worker));

  return {
    catalog: Object.assign({}, catalog, {
      videos: videos.filter(function (video) { return kept[video.id]; })
    }),
    dropped: dropped
  };
}

function isUrl(value) {
  return /^https?:\/\//i.test(value);
}

function isTar(value) {
  return /\.(tar|tgz|tar\.gz)$/i.test(value);
}

function download(url, dest, opts) {
  return new Promise(function (resolve, reject) {
    const req = https.get(url, {rejectUnauthorized: !(opts && opts.insecure)}, function (res) {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        resolve(download(new URL(res.headers.location, url).toString(), dest, opts));
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error('HTTP ' + res.statusCode + ' for ' + url));
        return;
      }
      const file = fs.createWriteStream(dest);
      res.pipe(file);
      file.on('finish', function () { file.close(function () { resolve(dest); }); });
      file.on('error', reject);
    });
    req.on('error', reject);
  });
}

function findEntriesJson(dir) {
  const direct = path.join(dir, 'entries.json');
  if (fs.existsSync(direct)) return direct;
  const stack = [dir];
  while (stack.length) {
    const current = stack.pop();
    for (const name of fs.readdirSync(current)) {
      const full = path.join(current, name);
      if (fs.statSync(full).isDirectory()) stack.push(full);
      else if (name === 'entries.json') return full;
    }
  }
  throw new Error('entries.json not found in archive');
}

// Resolves a --source (URL or local path, tar or json) to a parsed feed object.
async function readEntries(source, opts) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'aerial-feed-'));
  try {
    let local = source;
    if (isUrl(source)) {
      local = path.join(temp, isTar(source) ? 'feed.tar' : 'entries.json');
      await download(source, local, opts);
    } else if (!fs.existsSync(source)) {
      throw new Error('Source not found: ' + source);
    }

    let entriesPath = local;
    if (isTar(local)) {
      execFileSync('tar', ['-xf', local, '-C', temp], {stdio: 'ignore'});
      entriesPath = findEntriesJson(temp);
    }
    return JSON.parse(fs.readFileSync(entriesPath, 'utf8'));
  } finally {
    fs.rmSync(temp, {recursive: true, force: true});
  }
}

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (value == null || value.indexOf('--') === 0) {
    throw new Error(flag + ' expects a value');
  }
  return value;
}

function parseArgs(argv) {
  const args = {all: false, verify: false, insecure: false};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--all') args.all = true;
    else if (arg === '--verify') args.verify = true;
    else if (arg === '--insecure') args.insecure = true;
    else if (arg === '--help' || arg === '-h') args.help = true;
    else if (arg === '--source') args.source = requireValue(argv, ++i, arg);
    else if (arg === '--out') args.out = requireValue(argv, ++i, arg);
    else if (arg === '--limit') {
      const limit = parseInt(requireValue(argv, ++i, arg), 10);
      if (!Number.isFinite(limit) || limit <= 0) throw new Error('--limit expects a positive integer');
      args.limit = limit;
    } else throw new Error('Unknown argument: ' + arg);
  }
  return args;
}

function usage() {
  return [
    'Usage: node scripts/update-aerials.js [options]',
    '',
    '  --source <url|path>  Feed to read (default: Apple tvOS 26 aerials)',
    '  --out <path>         Output file (default: src/js/aerial-videos.json)',
    '  --all                Include clips not in shuffle / top level',
    '  --limit <n>          Keep only the first n clips after ordering',
    '  --verify             HEAD-check URLs and drop clips with dead links',
    '  --insecure           Skip TLS verification (Apple serves an incomplete chain)'
  ].join('\n');
}

async function run(argv) {
  const args = parseArgs(argv);
  if (args.help) {
    console.log(usage());
    return;
  }

  const source = args.source || DEFAULT_SOURCE;
  const out = args.out || DEFAULT_OUT;

  const feed = await readEntries(source, {insecure: args.insecure});
  let catalog = buildCatalog(feed, {all: args.all, limit: args.limit, source: source});

  if (args.verify) {
    const result = await verifyCatalog(catalog, {headOptions: {insecure: args.insecure}});
    catalog = result.catalog;
    console.log('verify: dropped ' + result.dropped.length + ' clip(s) with dead links');
  }

  if (!catalog.videos.length) {
    throw new Error('No aerial videos matched — refusing to write an empty catalog');
  }

  fs.mkdirSync(path.dirname(out), {recursive: true});
  fs.writeFileSync(out, JSON.stringify(catalog, null, 2) + '\n');
  console.log('Wrote ' + catalog.videos.length + ' aerial videos to ' + path.relative(process.cwd(), out));
}

if (require.main === module) {
  run(process.argv.slice(2)).catch(function (err) {
    console.error(err.message || err);
    if (/certificate|issuer|self.signed|SELF_SIGNED|UNABLE_TO_GET_ISSUER/i.test(String(err.message || err))) {
      console.error('\nThe feed host serves an incomplete TLS chain. Run it via `npm run update:aerials`');
      console.error('(which uses node --use-system-ca), or pass --insecure to skip verification.');
    }
    process.exit(1);
  });
}

module.exports = {buildCatalog, verifyCatalog, readEntries, DEFAULT_SOURCE, DEFAULT_OUT};
