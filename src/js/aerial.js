import aerialData from './aerial-videos.json' with {type: 'json'};

/**
 * Aerial-video background core: the runtime catalog (generated Apple aerials +
 * a hand-curated drone fallback), clip lookup, and the pure quality resolver
 * that maps a panel's capabilities to one concrete source URL.
 *
 * No video bytes ship in the .ipk — only metadata (ids, titles, posters, stream
 * URLs). See docs/background-sources.md and scripts/update-aerials.js.
 */

/** Quality modes offered in Settings (see videoQuality in config.js). */
export const AERIAL_QUALITIES = ['auto', 'uhd-hdr', 'uhd-sdr', 'hd-h264'];
export const DEFAULT_VIDEO_QUALITY = 'auto';

// The three source variants, best first. These are the keys of a clip's
// `sources` object and the ladder the resolver walks.
const QUALITY_LADDER = ['uhdHdr', 'uhdSdr', 'hdH264'];
const QUALITY_TO_KEY = {
  auto: 'auto',
  'uhd-hdr': 'uhdHdr',
  'uhd-sdr': 'uhdSdr',
  'hd-h264': 'hdH264'
};

/**
 * Hand-curated free-drone fallback (Pexels; free licence, hotlinked only).
 * Kept separate from the generated catalog so `update:aerials` never wipes it.
 * Entries use the same shape as the generated ones. Populated in a follow-up
 * ticket — the mechanism ships first.
 */
export const DRONE_FALLBACK = [];

function generatedVideos() {
  return aerialData && Array.isArray(aerialData.videos) ? aerialData.videos : [];
}

/** The clips the runtime can play: generated Apple aerials + the drone fallback. */
export function getAerialCatalog() {
  return generatedVideos().concat(DRONE_FALLBACK);
}

/** A clip by id, or null when it is unknown/empty (a stale saved id, say). */
export function findAerialVideoById(id) {
  if (!id) return null;
  const catalog = getAerialCatalog();
  for (let i = 0; i < catalog.length; i += 1) {
    if (catalog[i] && catalog[i].id === id) return catalog[i];
  }
  return null;
}

/**
 * The concrete source variant to use, given the panel's capabilities.
 *
 * `auto` climbs the ladder: HDR when the panel is HDR-capable, else 4K SDR
 * when HEVC decodes, else 1080p H.264. An explicit mode forces its variant and
 * falls through the lower rungs when the panel can't take it. Performance mode
 * always resolves to 1080p H.264. Returns '' when nothing is playable.
 *
 * @param {{quality?: string, perfMode?: boolean, canPlayHevc?: boolean, hdrCapable?: boolean}} opts
 * @returns {'uhdHdr'|'uhdSdr'|'hdH264'|''}
 */
export function resolveAerialQuality(opts) {
  const options = opts || {};
  const canPlayHevc = !!options.canPlayHevc;
  const hdrCapable = !!options.hdrCapable;
  const capable = {
    // HDR is HEVC-coded too: a panel that can't decode HEVC can't show it.
    uhdHdr: hdrCapable && canPlayHevc,
    uhdSdr: canPlayHevc,
    hdH264: true
  };

  if (options.perfMode) return 'hdH264';

  const mode = QUALITY_TO_KEY[options.quality] || 'auto';
  const ladder = mode === 'auto' || mode === 'uhdHdr' ? QUALITY_LADDER
    : mode === 'uhdSdr' ? ['uhdSdr', 'hdH264']
      : ['hdH264'];

  for (let i = 0; i < ladder.length; i += 1) {
    if (capable[ladder[i]]) return ladder[i];
  }
  return '';
}

/**
 * The URL to play for a clip's `sources`, walking down from the resolved
 * variant so an empty HDR/4K URL still yields the best available fallback.
 */
export function resolveAerialSourceUrl(sources, opts) {
  const resolved = resolveAerialQuality(opts);
  if (!resolved || !sources) return '';
  const start = QUALITY_LADDER.indexOf(resolved);
  for (let i = start; i < QUALITY_LADDER.length; i += 1) {
    const url = sources[QUALITY_LADDER[i]];
    if (url) return url;
  }
  return '';
}

/**
 * Which clips the user picked: a single `videoId`, a `videoIds` subset, or —
 * when neither is set (empty array) — the whole catalog. Unknown ids are
 * dropped rather than breaking playback.
 */
export function resolveAerialSelection(bg) {
  const cfg = normalizeVideoConfig(bg);
  if (cfg.videoId) {
    const one = findAerialVideoById(cfg.videoId);
    if (one) return [one];
  }
  if (cfg.videoIds.length) {
    return cfg.videoIds.map(findAerialVideoById).filter(Boolean);
  }
  return getAerialCatalog();
}

/** Normalize the aerial-video fields of a background config. */
export function normalizeVideoConfig(bg) {
  const out = Object.assign({}, bg || {});
  if (typeof out.videoId !== 'string') out.videoId = '';
  if (!Array.isArray(out.videoIds)) out.videoIds = [];
  out.videoIds = out.videoIds.filter(function (id) {
    return typeof id === 'string' && id;
  });
  if (AERIAL_QUALITIES.indexOf(out.videoQuality) < 0) {
    out.videoQuality = DEFAULT_VIDEO_QUALITY;
  }
  return out;
}

function detectHevc() {
  try {
    const video = document.createElement('video');
    return !!(video.canPlayType('video/mp4; codecs="hvc1"') ||
      video.canPlayType('video/mp4; codecs="hev1"'));
  } catch (err) {
    return false;
  }
}

function detectHdr() {
  try {
    return !!(window.matchMedia && window.matchMedia('(dynamic-range: high)').matches);
  } catch (err) {
    return false;
  }
}

/**
 * What the panel can play. HEVC comes from `canPlayType`; HDR from
 * `matchMedia('(dynamic-range: high)')`, with an optional async `probe` (the
 * Luna display check, wired by the playback layer) as a fallback when
 * matchMedia isn't supported.
 *
 * @param {() => Promise<boolean>|boolean} [probe]
 */
export async function detectAerialCapabilities(probe) {
  const canPlayHevc = detectHevc();
  let hdrCapable = detectHdr();
  if (!hdrCapable && typeof probe === 'function') {
    try {
      hdrCapable = !!(await probe());
    } catch (err) {
      hdrCapable = false;
    }
  }
  return {canPlayHevc: canPlayHevc, hdrCapable: hdrCapable};
}
