# webOS Launch Home

A fullscreen home screen for rooted LG webOS TVs. Pick an app, switch inputs, and enjoy ambient background music — without the stock launcher clutter.

![Launch Home on an LG TV](docs/screenshots/screengrab1.jpg)

![Launch Home settings](docs/screenshots/screengrab2.jpg)

## Features

- App grid with pinned streaming apps, plus custom app tiles (pin any installed app by App ID with a bundled icon)
- HDMI and TV input shortcuts with custom labels (uncheck all inputs to hide the row entirely); add any input the TV doesn't list (Settings → Inputs & channels → Add an input)
- Scenic backgrounds (built-in, USB, or **online nature + anime URLs** — no extra images in the IPK) and built-in ambient music that keeps playing while settings are open
- Frosted-glass tiles in a colour of your choice (Settings → Look → Glass colour: light, dark, black or tinted) to suit dark or bright wallpapers
- Large centered clock with optional date, both independently toggleable, 24-hour or 12-hour
- Weather for today and the next 4 days (Open-Meteo, no API key), with a city search and °C/°F in Settings
- Compact volume control; optional music bar (track name) in Settings
- **TV channels** — when the TV has Live TV channels tuned, a Channels button after the inputs opens a strip above them with what's on now and your LG favourite channels (or all of them); pick one to watch it
- Optional TV system volume levels for Launch Home vs when apps launch (default “Don’t change” keeps the volume you set with the remote)
- Adjustable icon size and left/center/right icon alignment
- Launch Home icon set for popular apps, or switch it off to use each app’s own TV icon (needs root)
- Dedicated app settings button and a TV Settings tile for quick access to system settings
- **Add all apps from LG's home screen** in one press (Settings → Apps → Add an app), in LG's order, or pick installed apps one by one; no app ids to type
- **Launch on Home button** — root watcher reopens Launch Home when stock Home appears
- **Boot on TV start** — root init.d script launches Launch Home after power-on
- **Backup & restore** — Settings → Tools saves all your Launch Home settings to the TV (kept when you reinstall Launch Home) and to a plugged-in USB drive (`lounge/launch-home-settings.json`) to move them to another TV. Launch Home also keeps its own copy before each restore and each update, and backups made by other versions restore as far as they fit, telling you what didn't (see [Backup & restore](#backup--restore)); needs root
- **Voice assistant (optional)** — built in: the Magic Remote's Voice button answers with Grok, Gemini or OpenRouter, opens apps and controls the TV (see [Voice](#voice))
- Remote-friendly navigation. Settings is a category list (Look, Music, Screensaver, Apps, Inputs & channels, Weather, System, AI Voice, Tools): Up/Down picks a category, Right or OK enters it, Left or Back returns to the list

## Voice

Launch Home has its own voice assistant for the Magic Remote's **Voice** button, built in and **off by default**. Turn it on in **Settings → AI Voice → Voice assistant** (needs root through Homebrew Channel). Then add an xAI (Grok) key or sign in with SuperGrok, or use a Gemini or OpenRouter key, and press Save.

**What you can say:** open apps (“open Netflix”), volume up/down/mute or a level, channel up/down or a number, switch inputs (“HDMI 2”), captions on/off, a sleep timer, “turn off the TV”, the weather, and any other question, which is answered on screen and spoken aloud.

**While it's on, Launch Home:**

- Shows a small **mic badge** top-right while it listens.
- Opens the apps it asks for from the foreground (the reliable way to bring native apps such as Prime Video to the front).
- Runs its daemon (`voice/daemon`, Python 3, runs as root) from the Launch Home app folder, restarts it after updates, and starts it at power-on (`/var/lib/webosbrew/init.d/45-launch-home-voice`).
- Installs a small hidden app, **Launch Home Voice** (`org.webosbrew.lounge.voice`), that shows the question and answer and plays the spoken reply.

Turning it off stops the daemon, removes the boot hook and the voice card, and gives the Voice button back to LG. Your settings and SuperGrok sign-in stay in `/home/root/.config/launch-home-voice` for next time. The log is `/tmp/launch-home-voice.log`.

**Other voice assistants.** Everything above belongs to Launch Home alone: its own port (`127.0.0.1:8678`), files, settings and app ids. Launch Home only talks to a voice service that identifies itself as its own, and never stops or changes another assistant such as VoxRelay. If VoxRelay is also running or installed, the AI Voice tab warns you, because both would answer the Voice button.

**Interface** (between Launch Home and its daemon). On `ws://127.0.0.1:8678` Launch Home first sends `{"type": "hello", "params": {"role": "launcher"}}` and continues only if the reply names `launch-home-voice`. The daemon sends JSON `{"event": …, "payload": …}`:

| Event | Payload | Launch Home |
|---|---|---|
| `sessionStarted`, `sessionCatchup` | `listening: true` (or `early: true` / `reason: "button_press"`) | show mic badge |
| `listeningEnded`, `sessionEnded`, `error` | — | hide mic badge |
| `appLaunch` | `id`, `ids[]`, `spoken` | launch that app |
| `transcriptFinal` | `text` | "open …/launch …" matched against the dock |

AI Voice settings are requests `{"type": …, "params": …, "id": …}`, answered with `{"event": "configResult", "id", "ok", "result"|"error"}`. The requests are `getConfig` (values plus `options`, the picker choices), `setConfig`, `getStatus`, and the SuperGrok sign-in requests `startSuperGrokLogin`, `cancelSuperGrokLogin`, `signOutSuperGrok` and `importSuperGrokAuth`.

## Backup & restore

**Settings → Tools → Backup & restore** (needs root through Homebrew Channel):

- **Back up settings** saves everything you set in Launch Home to `/home/root/.config/launch-home/settings-backup.json` on the TV, and to `lounge/launch-home-settings.json` on a plugged-in USB drive. Voice keys and the SuperGrok sign-in aren't included; they stay in `/home/root/.config/launch-home-voice`.
- **Restore settings** lists every backup it finds: yours, the USB drive's, and the copies Launch Home keeps by itself in `/home/root/.config/launch-home/auto/` (one from before your last restore, and the settings from before each of the last three updates). Pick one and press **Restore this backup**.

Launch Home is young and its settings still change between versions, so a backup made by one version can be restored by another:

- Each backup file records the Launch Home version that wrote it and its settings layout version.
- **Older backup:** its settings are updated by the same steps Launch Home uses when you update it.
- **Newer backup:** settings this version doesn't have are kept but not used.
- A setting that doesn't fit (for example, a number where this version expects text) keeps your current value. After a restore, the status line lists every setting that was skipped or not used.
- The voice assistant's on/off setting belongs to each TV and isn't restored.
- To undo a restore, restore the copy marked **before your last restore**. To go back to an older Launch Home, restore the copy marked **before updating** that it wrote.

## Compatibility

| webOS version | Status | Notes |
| --- | --- | --- |
| webOS 25 (sdk ~10) | Working | Primary test platform — LG OLED55C56LB |
| webOS 6–9 / 22–24 | Expected working | Same Luna APIs as 25; not fully regression-tested here |
| webOS 5.x | Expected working | Use project-local `@webos-tools/cli` for packaging (epoch tar fix) |
| webOS 4.x | Working (reported) | Home watcher + boot-on-start use webOS 4–safe `luna-send` fallbacks; backgrounds viewable on device (see below) |

**Requirements:** rooted TV with [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel) and SSH. Root elevation is required for full app scanning, Home-button intercept, and boot-on-start.

### webOS 4.x notes

- **Background JPEGs on device.** Built-in scenic images ship inside the app package. On webOS 4 you can browse them directly under:

  ```text
  /media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/assets/backgrounds/
  ```

  (Some file managers list this as `…/assets/background`.)

- **Online backgrounds.** Settings → Background → **Online URL (nature + anime)** opens a thumbnail gallery of curated Unsplash nature photos plus popular free anime-style wallpapers (or paste your own https image URL). Photos load over the network so the package stays small. See [docs/background-sources.md](docs/background-sources.md).

- **Home button / Boot on start.** Both install hooks under `/var/lib/webosbrew/init.d/` via the elevated Homebrew Channel service. Toggle the setting **off → Save → on → Save** after an update if either stops working. Confirm Homebrew startup is installed (see [Running elevated as root](#running-elevated-as-root-required-for-app-scanning)).

## Install

Requires a rooted LG TV with [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel) and SSH enabled.

```bash
npm install
./install2tvfrommacos.sh
```

Set your TV's IP if needed:

```bash
TV_IP=192.168.0.79 ./install2tvfrommacos.sh
```

Or build manually:

```bash
npm run pack
ares-install --device webos dist/*.ipk
```

`npm run pack` uses the project-local `@webos-tools/cli` and rejects files dated `1970-01-01`. That epoch stamp is a `@webosose/ares-cli` + Node.js 22+ bug; some TVs (webOS 5) refuse to install those packages. Do not package with `@webosose/ares-cli` on modern Node.

## Running elevated as root (required for app scanning)

**Why this is required.** Retail webOS only returns the *full* list of installed
apps (`luna://com.webos.applicationManager/listApps`) to **privileged (root)
clients**. A normal sandboxed web app can only see its own launch points, so the
built-in **Scan for apps** feature returns nothing unless the launcher runs with
elevated (root) Luna privileges. On a rooted TV that elevation is provided by the
[Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel) root
service (`luna://org.webosbrew.hbchannel.service/exec`), which executes as root.

Home-button intercept and Boot on TV start use the same root service to install
`/var/lib/webosbrew/init.d/` hooks. Those hooks run when the TV boots, and with
**Quick Start+** on the TV only wakes from standby, so they don't run until a
full restart. Turn Quick Start+ off under General → Devices → TV Management.

### 1. Elevate (grants root)

SSH into the TV (the installer already provisions `root@TV_IP` key auth) and run
the Homebrew Channel elevation helper:

```bash
ssh root@TV_IP
/media/developer/apps/usr/palm/services/org.webosbrew.hbchannel.service/elevate-service
```

### 2. Persist across reboots and app updates

Copy the Homebrew Channel startup script into the boot location so elevation is
re-applied automatically on every boot:

```bash
cp /media/developer/apps/usr/palm/services/org.webosbrew.hbchannel.service/startup.sh \
   /var/lib/webosbrew/startup.sh
```

This lives **outside** the app directory
(`/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher`), so
reinstalling or updating the Launch Home `.ipk` does **not** remove it —
root elevation survives app updates.

### 3. (Optional) Force re-elevation on every boot

Homebrew Channel runs any executable placed in `/var/lib/webosbrew/init.d` at
boot as root. Add a hook so the service is always re-elevated after an update:

```bash
mkdir -p /var/lib/webosbrew/init.d
cat > /var/lib/webosbrew/init.d/30-lounge-elevate <<'EOF'
#!/bin/sh
/media/developer/apps/usr/palm/services/org.webosbrew.hbchannel.service/elevate-service
EOF
chmod +x /var/lib/webosbrew/init.d/30-lounge-elevate
```

> Note: A full **TV firmware update** can reset root. If app scanning stops
> working after a system update, re-root the TV / reinstall Homebrew Channel and
> repeat steps 1–2.

## License

MIT
