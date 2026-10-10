# Background image sources

Launch Home can show backgrounds from:

1. **Built-in photos** — 5 premium **~3840px** JPEGs packaged under `assets/backgrounds/` (offline), sharp on 4K TVs. Online URL streams additional ~3840px photos without growing the package further. With **Performance mode** on, Launch Home uses 1920px versions instead, about a quarter of the pixels to decode, for slower TVs: online photos are requested at 1920px, and the built-ins' 1920px copies (`assets/backgrounds/1920/`, not packaged in the IPK) are downloaded from this repo via jsDelivr and cached. Offline, the packaged 4K originals are used.
2. **USB folder** — files on a stick (`lounge/backgrounds/` + optional `images.json`)
3. **Online URL** — direct `https://` image links (Unsplash nature, Wallhaven anime, Pexels, your CDN) — **not** stored in the package
4. **Aerial videos** — muted, full-bleed Apple TV aerials (hotlinked, never packaged), with a separate hand-curated free-drone fallback list the updater never overwrites — **not** stored in the package
5. **Gradients** — CSS presets (no images)

Prefer **Online URL** or **Aerial videos** when you want more variety without growing the IPK.

## Using online photos in the app

1. Open **Settings → Background**
2. Set **Source** to the catalog you want (only that gallery is shown):
   - **Built-in photos** → **Choose a built-in photo** thumbnail grid (packaged JPEGs)
   - **Online URL (nature + anime)** → **Choose an online photo** grid (nature + anime), or **Custom URL** + **Image URL**
3. For slideshow: set **Display** to **Slideshow**. Leave **Slideshow URLs** empty (online) to cycle the curated remote set, or paste one `https://` URL per line.
4. Built-in and online selections are independent — picking Mountain Sunset (built-in) does not change the online pick, and the online gallery is hidden while Source is Built-in.

**Requirements:** TV must have network access. Use **direct image URLs** (ending in a real JPEG/WebP response), not HTML gallery pages.

**Fallback:** If a remote image fails to load (offline, TLS, 404), the launcher falls back to a gradient after a short timeout.

## Aerial videos

**Aerial videos** puts moving scenery behind the launcher: Apple TV aerials (hotlinked, never packaged), with a separate hand-curated free-drone fallback list in `src/js/aerial.js` (`DRONE_FALLBACK`) that `update:aerials` never overwrites. Clips play muted, full-bleed and looping, like a wallpaper rather than a player.

1. Open **Settings → Background** and set **Source** to **Aerial videos**.
2. **Choose aerial clips** — a poster gallery of every clip (each with a small ▶ marker). All clips are selected by default; OK toggles a clip, so you can narrow the rotation to a handful.
3. **Aerial quality** — how the clip is fetched:
   - **Auto** (default): 4K HDR when the TV supports it, else 4K SDR when it decodes HEVC, else 1080p H.264.
   - **4K HDR** / **4K SDR** / **1080p**: force a variant; an unsupported choice falls through to the next playable one.
   - **Performance mode** always uses 1080p H.264.
4. **Display** — **Single image** loops the first selected clip; **Slideshow** rotates through the selected clips, advancing when each clip ends.

The video pauses while the in-app Settings panel is open, while another app or a TV overlay is in front, and while Launch Home is backgrounded, and resumes on return. Reduced-motion TVs show a still poster. If no clip can play (offline, blocked, dead links), the launcher falls back to a gradient.

Apple's 4K clips are ~200–300 MB each and need HEVC decode; HDR also depends on the TV passing the PQ/HLG metadata to its decoder, and forcing HDR onto an SDR panel can look washed out. The automatic quality ladder, the explicit setting, the 1080p fallback, poster-while-buffering and skip-on-failure all exist because of that. Apple aerials are hotlinked under Apple's terms; the drone fallback uses the Pexels free licence. **No video bytes ship in the `.ipk`.**

### Refreshing the aerial list

Apple's feed URL is versioned and moves over time. Regenerate the committed catalog with:

```bash
npm run update:aerials
```

It downloads Apple's aerial feed, extracts `entries.json`, keeps the curated shuffle/top-level clips (deduped by `shotID`, ordered by `preferredOrder`) and writes `src/js/aerial-videos.json`. The list is committed and bundled, so the TV makes no extra request and offline launchers still show the gallery (the clips themselves are still streamed).

Flags (pass them after `--`, e.g. `npm run update:aerials -- --limit 40`):

- `--source <url|path>` — a fresher Apple feed, a mirror, or a local `.tar`/`.tgz`/`entries.json`. Default: Apple's tvOS aerial feed (see the header of `scripts/update-aerials.js`).
- `--all` — keep every clip, not just shuffle/top-level.
- `--limit N` — keep only the first N clips after ordering.
- `--verify` — HEAD-check each clip and poster and drop dead entries before writing (off by default).
- `--out <path>` — write the catalog somewhere other than `src/js/`.
- `--insecure` — skip TLS verification (for a mirror with a bad certificate).

## Curated remote set

**39 online-only photos** in `src/js/backgrounds.js` (`REMOTE_BACKGROUNDS`):

| Set | Count | Host | Notes |
|-----|------:|------|--------|
| Luxury tropical beaches | 4 | Unsplash CDN | Palms, sun, shoreline — first in the online gallery |
| SpaceX rockets | 2 | X image CDN, Unsplash CDN | Starship launch at Starbase (an image posted on X, `pbs.twimg.com/media/HTU8ja-XgAE6GwQ`); Falcon Heavy launch (SpaceX on Unsplash) |
| Nature / travel | 23 | Unsplash / Wallhaven | Scenic remote set (no alpine snow) |
| Anime girls | 12 | Wallhaven CDN | Popular SFW smiling face / fun portraits (`w.wallhaven.cc`) |

They are **not** the built-in pack — Settings → **Online URL** shows only this network gallery (plus Custom URL). Built-in photos appear only under **Built-in photos**.

No image bytes are shipped in the package; the TV loads them over HTTPS.

To extend or replace the set, edit `REMOTE_BACKGROUNDS` (id, title, direct `https://…` image URL).

**Licenses / usage**

- Unsplash: [Unsplash License](https://unsplash.com/license) (free commercial use; no attribution required).
- Wallhaven anime set: free wallpaper downloads via the public CDN (user-uploaded art). Fine for personal TV wallpapers; not an Unsplash-style commercial stock license. Hotlinked only (not redistributed in the `.ipk`).
- Starship launch: SpaceX launch photography as posted on X, hotlinked from X's image CDN (not redistributed in the `.ipk`). It stays available only while the post does.

## Adding your own remote URLs

Any host works if it serves a **direct** image over HTTPS, for example:

- Unsplash: `https://images.unsplash.com/photo-…?w=3840&q=92&auto=format&fit=crop`
- Pexels: `https://images.pexels.com/photos/…/pexels-photo-….jpeg?auto=compress&cs=tinysrgb&w=3840`
- Wallhaven: `https://w.wallhaven.cc/full/ab/wallhaven-abcdef.jpg` (settings thumbs map automatically)
- Your own server / GitHub raw / S3, etc.

To extend the in-app picker, add entries to `REMOTE_BACKGROUNDS` in `src/js/backgrounds.js` (id, title, url only). For more anime picks, use Wallhaven’s SFW anime filter and paste the full image URL.

## Why not package more JPEGs?

Each 4K wallpaper is ~1–3 MB. Streaming from a CDN keeps the `.ipk` smaller and lets you change the catalog without rebuilding assets. Built-ins remain available for offline TVs.
