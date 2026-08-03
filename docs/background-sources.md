# Background image sources

Launch Home can show backgrounds from:

1. **Built-in photos** — 5 premium **~3840px** JPEGs packaged under `assets/backgrounds/` (offline), sharp on 4K TVs. Online URL streams additional ~3840px photos without growing the package further.
2. **USB folder** — files on a stick (`lounge/backgrounds/` + optional `images.json`)
3. **Online URL** — direct `https://` image links (Unsplash nature, Wallhaven anime, Pexels, your CDN) — **not** stored in the package
4. **Gradients** — CSS presets (no images)

Prefer **Online URL** when you want more variety without growing the IPK.

## Using online photos in the app

1. Open **Settings → Background**
2. Set **Source** to the catalog you want (only that gallery is shown):
   - **Built-in photos** → **Choose a built-in photo** thumbnail grid (packaged JPEGs)
   - **Online URL (nature + anime)** → **Choose an online photo** grid (nature + anime), or **Custom URL** + **Image URL**
3. For slideshow: set **Display** to **Slideshow**. Leave **Slideshow URLs** empty (online) to cycle the curated remote set, or paste one `https://` URL per line.
4. Built-in and online selections are independent — picking Mountain Sunset (built-in) does not change the online pick, and the online gallery is hidden while Source is Built-in.

**Requirements:** TV must have network access. Use **direct image URLs** (ending in a real JPEG/WebP response), not HTML gallery pages.

**Fallback:** If a remote image fails to load (offline, TLS, 404), the launcher falls back to a gradient after a short timeout.

## Curated remote set

**39 online-only photos** in `src/js/backgrounds.js` (`REMOTE_BACKGROUNDS`):

| Set | Count | Host | Notes |
|-----|------:|------|--------|
| Luxury tropical beaches | 4 | Unsplash CDN | Palms, sun, shoreline — first in the online gallery |
| Nature / travel | 23 | Unsplash / Wallhaven | Scenic remote set (no alpine snow) |
| Anime girls | 12 | Wallhaven CDN | Popular SFW smiling face / fun portraits (`w.wallhaven.cc`) |

They are **not** the built-in pack — Settings → **Online URL** shows only this network gallery (plus Custom URL). Built-in photos appear only under **Built-in photos**.

No image bytes are shipped in the package; the TV loads them over HTTPS.

To extend or replace the set, edit `REMOTE_BACKGROUNDS` (id, title, direct `https://…` image URL).

**Licenses / usage**

- Unsplash: [Unsplash License](https://unsplash.com/license) (free commercial use; no attribution required).
- Wallhaven anime set: free wallpaper downloads via the public CDN (user-uploaded art). Fine for personal TV wallpapers; not an Unsplash-style commercial stock license. Hotlinked only (not redistributed in the `.ipk`).

## Adding your own remote URLs

Any host works if it serves a **direct** image over HTTPS, for example:

- Unsplash: `https://images.unsplash.com/photo-…?w=3840&q=92&auto=format&fit=crop`
- Pexels: `https://images.pexels.com/photos/…/pexels-photo-….jpeg?auto=compress&cs=tinysrgb&w=3840`
- Wallhaven: `https://w.wallhaven.cc/full/ab/wallhaven-abcdef.jpg` (settings thumbs map automatically)
- Your own server / GitHub raw / S3, etc.

To extend the in-app picker, add entries to `REMOTE_BACKGROUNDS` in `src/js/backgrounds.js` (id, title, url only). For more anime picks, use Wallhaven’s SFW anime filter and paste the full image URL.

## Why not package more JPEGs?

Each 4K wallpaper is ~1–3 MB. Streaming from a CDN keeps the `.ipk` smaller and lets you change the catalog without rebuilding assets. Built-ins remain available for offline TVs.
