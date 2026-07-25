# Background image sources

Lounge can show backgrounds from:

1. **Built-in photos** — JPEGs packaged in the `.ipk` under `assets/backgrounds/` (offline), including beach scenes (Tropical Beach, Azure Cove, Palm Shore) and scenic landscapes
2. **USB folder** — files on a stick (`lounge/backgrounds/` + optional `images.json`)
3. **Online URL** — direct `https://` image links (Unsplash, Pexels, your CDN) — **not** stored in the package
4. **Gradients** — CSS presets (no images)

Prefer **Online URL** when you want more variety without growing the IPK.

## Using online photos in the app

1. Open **Settings → Background**
2. Set **Source** to the catalog you want (only that gallery is shown):
   - **Built-in photos** → **Choose a built-in photo** thumbnail grid (packaged JPEGs)
   - **Online URL (Unsplash / Pexels)** → **Choose an online photo** grid, or **Custom URL** + **Image URL**
3. For slideshow: set **Display** to **Slideshow**. Leave **Slideshow URLs** empty (online) to cycle the curated remote set, or paste one `https://` URL per line.
4. Built-in and online selections are independent — picking Mountain Sunset (built-in) does not change the online pick, and the online gallery is hidden while Source is Built-in.

**Requirements:** TV must have network access. Use **direct image URLs** (ending in a real JPEG/WebP response), not HTML gallery pages.

**Fallback:** If a remote image fails to load (offline, TLS, 404), the launcher falls back to a gradient after a short timeout.

## Curated remote set (Unsplash CDN)

**24 online-only photos** in `src/js/backgrounds.js` (`REMOTE_BACKGROUNDS`).  
They are **not** the built-in pack — Settings → **Online URL** shows only this network gallery (plus Custom URL). Built-in photos appear only under **Built-in photos**.

No image bytes are shipped in the package; the TV loads them over HTTPS.

To extend or replace the set, edit `REMOTE_BACKGROUNDS` (id, title, direct `https://images.unsplash.com/…` URL).

License: [Unsplash License](https://unsplash.com/license).

## Adding your own remote URLs

Any host works if it serves a **direct** image over HTTPS, for example:

- Unsplash: `https://images.unsplash.com/photo-…?w=3840&q=85&auto=format&fit=crop`
- Pexels: `https://images.pexels.com/photos/…/pexels-photo-….jpeg?auto=compress&cs=tinysrgb&w=3840`
- Your own server / GitHub raw / S3, etc.

To extend the in-app picker, add entries to `REMOTE_BACKGROUNDS` in `src/js/backgrounds.js` (id, title, url only).

## Why not package more JPEGs?

Each 4K wallpaper is ~1–3 MB. Streaming from a CDN keeps the `.ipk` smaller and lets you change the catalog without rebuilding assets. Built-ins remain available for offline TVs.
