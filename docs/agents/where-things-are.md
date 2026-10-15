# Where things are

The app is plain ES modules, bundled by `scripts/build.js` (esbuild) into
`dist/` and packed into an `.ipk`. There is no framework.

## Home screen (`src/js/`)

| File | What it does |
| --- | --- |
| `main.js` | Wires everything up: start-up, returns from apps, the remote handlers, the Home button, volume levels, the screensaver timer. |
| `focus.js` | The remote: which control the D-pad lands on, in the home rows and in Settings. Spatial moves, back-tracking, Settings' category list. |
| `apps.js`, `app-catalog.js`, `app-icons.js`, `all-apps.js` | The app tiles: the TV's app list (read as root), remembered names and icons, bundled icons, the All apps grid. |
| `inputs.js`, `channels.js` | The HDMI/input chips and the Live TV channel strip. |
| `background.js`, `backgrounds.js`, `aerials.js` | The wallpaper: built-in photos, USB, online URLs, Apple aerial clips; the slideshow; the online catalogs. |
| `screensaver.js` | The in-app screensaver (photos or aerials, clock). |
| `music.js`, `builtin-music.js` | Ambient music and the music bar. |
| `settings.js` | The whole Settings panel: one category list, one section per feature, Save writes the config. |
| `config.js` | Defaults, the stored config, migrations (`CONFIG_SCHEMA_VERSION`). |
| `settings-backup.js` | Backup and restore, with version compatibility. |
| `luna.js` | Every call to the TV: Luna services, root through Homebrew Channel, app launching, volume, inputs, channels. |
| `clock-format.js`, `weather.js`, `profiles.js`, `usb.js`, `compat.js` | The clock, the weather panel, profiles (Night, Cinema), USB discovery, old-webOS shims. |

## Shipped scripts (`scripts/`, copied into the package)

`voice-run.sh`, `enable-voice.sh`, `disable-voice.sh`, `home-watcher.sh`
and the `enable-/disable-home-watcher.sh` pair, `boot-launch.sh`,
`channels.sh`, `diagnostics.sh` (the TV check). They run on the TV as root.
`deploy.sh`, `build.js`, `set-version.js` and `generate-aerials.js` are
developer tools and are not shipped.

## Voice assistant (`voice/`)

`voice/daemon/voice_daemon.py` and its modules run on the TV as a
background service (started by `voice-run.sh`); `voice/overlay/` is the
small card webOS shows while listening.

## Tests (`test/`)

vitest with jsdom: `npm test`. They cover pure logic and DOM behaviour
(focus routing, number keys, clip stepping). Nothing in them talks to a TV.

## Styles

`src/styles/main.css`, one file. The glass look is driven by CSS
variables on `body` (`glass-*`, `focus-*` classes set in `main.js`).
