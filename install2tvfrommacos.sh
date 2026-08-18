#!/usr/bin/env bash
#
# install2tvfrommacos.sh — Build and sideload Launch Home to a rooted LG TV.
#
# Usage:
#   ./install2tvfrommacos.sh
#   TV_IP=192.168.0.79 ./install2tvfrommacos.sh
#   TV_IP=192.168.0.79 LAUNCH=0 ./install2tvfrommacos.sh
#   SSH_ONLY=1 ./install2tvfrommacos.sh          # SSH setup only, no build/install
#
# Prerequisites:
#   - npm, @webos-tools/cli (local via npm install, or global)
#   - Rooted TV with Homebrew Channel SSH enabled (root@TV:22)
#   - TV and Mac on the same network
#
# Do not package with @webosose/ares-cli on Node.js 22+: it dates every
# file 1970-01-01 and some TVs (webOS 5) refuse to install that IPK.
#
# Environment:
#   TV_IP          TV IP address (default: 192.168.0.79)
#   DEVICE         ares device name (default: webos)
#   SSH_KEY        SSH key name under ~/.ssh (default: webos_deploy)
#   ROOT_PASSWORD  Root SSH password for first-time key setup (default: alpine)
#   LAUNCH         Launch app after install: 1 or 0 (default: 1)
#   SETUP_DEVICE   Run ares-setup-device: 1 or 0 (default: 1)
#   SSH_ONLY       Only set up SSH + ares device, skip build/install: 1 or 0 (default: 0)

set -euo pipefail

TV_IP="${TV_IP:-192.168.0.79}"
DEVICE="${DEVICE:-webos}"
SSH_KEY="${SSH_KEY:-webos_deploy}"
ROOT_PASSWORD="${ROOT_PASSWORD:-alpine}"
LAUNCH="${LAUNCH:-1}"
SETUP_DEVICE="${SETUP_DEVICE:-1}"
SSH_ONLY="${SSH_ONLY:-0}"

APP_ID="org.webosbrew.lounge.launcher"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEY_PATH="$HOME/.ssh/$SSH_KEY"
NODE_MAJOR="${NODE_MAJOR:-20}"

c_red() { printf '\033[31m%s\033[0m' "$*"; }
c_grn() { printf '\033[32m%s\033[0m' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m' "$*"; }
section() { printf "\n\033[1;36m━━ %s ━━\033[0m\n" "$*"; }
die() { printf "%s\n" "$(c_red "error: $*")" >&2; exit 1; }

ares_cmd() {
	local name="$1"
	shift
	local js
	js="$(ares_js "$name")"
	# @webosose/ares-cli on Node 22+ stamps IPK files 1970-01-01.
	if [[ "$js" == *"@webosose/ares-cli"* ]]; then
		printf "  %s\n" "$(c_ylw "Using @webosose/ares-cli via Node ${NODE_MAJOR} (Node 22+ dates IPK files 1970-01-01)")"
		node20 "$js" "$@"
	else
		node "$js" "$@"
	fi
}

ares_js() {
	local name="$1"
	local js roots

	js="$SCRIPT_DIR/node_modules/@webos-tools/cli/bin/${name}.js"
	if [[ -f "$js" ]]; then
		echo "$js"
		return 0
	fi

	roots="$(npm root -g 2>/dev/null || true)"
	for pkg in @webos-tools/cli @webosose/ares-cli; do
		js="$roots/$pkg/bin/${name}.js"
		if [[ -f "$js" ]]; then
			echo "$js"
			return 0
		fi
	done
	die "Could not find ${name}.js. Run npm install (needs @webos-tools/cli)."
}

node20() {
	local bin
	bin="$(type -P "node${NODE_MAJOR}" 2>/dev/null || true)"
	if [[ -n "$bin" ]]; then
		"$bin" "$@"
		return
	fi
	npx -y "node@${NODE_MAJOR}" "$@"
}

ssh_tv() {
	ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
		-i "$SSH_KEY_PATH" "root@${TV_IP}" "$@"
}

# This TV's /usr/bin/luna-send-pub is a very old (2011) build with different
# flag semantics than modern luna-send: the uri/message are POSITIONAL args,
# and "-i" means "interactive mode" (NOT "pass a uri"). It also fully buffers
# stdout when not attached to a tty, so a plain non-interactive SSH exec loses
# all output even though the call itself still runs. Route these through a
# pty (-tt) and use positional args so we can actually see (and verify) the
# result.
luna_send_tv() {
	ssh -tt -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no \
		-i "$SSH_KEY_PATH" "root@${TV_IP}" "$@" 2>&1
}

# webOS 6 TVs use standard luna-send (-i/-f). Some rooted newer TVs ship an old
# luna-send-pub build with positional uri/message args instead.
LUNA_SEND_STYLE=""

detect_luna_send_style() {
	if [[ -n "$LUNA_SEND_STYLE" ]]; then
		return 0
	fi

	if ssh_tv "test -x /usr/bin/luna-send-pub"; then
		if ssh_tv "/usr/bin/luna-send-pub -n 1 -w 5000 'luna://com.webos.service.tv.systemproperty/getSystemInfo' '{\"keys\":[\"sdkVersion\"]}'" 2>/dev/null \
			| grep -q '"returnValue":true'; then
			LUNA_SEND_STYLE="pub"
			return 0
		fi
	fi

	LUNA_SEND_STYLE="standard"
}

luna_subscribe_tv_cmd() {
	local uri="$1" payload="$2" replies="${3:-60}" wait_ms="${4:-90000}"
	detect_luna_send_style
	if [[ "$LUNA_SEND_STYLE" == "pub" ]]; then
		printf "/usr/bin/luna-send-pub -n %s -w %s '%s' '%s'" "$replies" "$wait_ms" "$uri" "$payload"
	else
		printf "luna-send -w %s -i '%s' '%s'" "$wait_ms" "$uri" "$payload"
	fi
}

luna_oneshot_tv() {
	local uri="$1" payload="$2" wait_ms="${3:-5000}"
	detect_luna_send_style
	if [[ "$LUNA_SEND_STYLE" == "pub" ]]; then
		ssh_tv "/usr/bin/luna-send-pub -n 1 -w ${wait_ms} '${uri}' '${payload}'" >/dev/null 2>&1 || true
	else
		ssh_tv "luna-send -n 1 -w ${wait_ms} -f '${uri}' '${payload}'" >/dev/null 2>&1 || true
	fi
}

luna_call_tv_cmd() {
	local uri="$1" payload="$2" wait_ms="${3:-15000}"
	detect_luna_send_style
	if [[ "$LUNA_SEND_STYLE" == "pub" ]]; then
		printf "/usr/bin/luna-send-pub -n 1 -w %s '%s' '%s'" "$wait_ms" "$uri" "$payload"
	else
		printf "luna-send -n 1 -w %s -f '%s' '%s'" "$wait_ms" "$uri" "$payload"
	fi
}

wait_for_ssh() {
	ssh_tv "true" >/dev/null 2>&1
}

read_version() {
	node "$SCRIPT_DIR/scripts/read-version.js"
}

ensure_deploy_key() {
	if [[ -f "$SSH_KEY_PATH" ]]; then
		printf "  %s\n" "$(c_grn "Using SSH key: $SSH_KEY_PATH")"
		return
	fi
	section "Creating deploy SSH key ($SSH_KEY)"
	ssh-keygen -t ed25519 -f "$SSH_KEY_PATH" -N "" -C "webos-deploy" -q
	printf "  %s\n" "$(c_grn "Created $SSH_KEY_PATH")"
}

install_key_on_tv() {
	if ssh -o BatchMode=yes -o ConnectTimeout=5 -i "$SSH_KEY_PATH" \
		-o StrictHostKeyChecking=no "root@${TV_IP}" "true" 2>/dev/null; then
		printf "  %s\n" "$(c_grn "SSH key auth already works")"
		return
	fi

	section "Installing SSH key on TV"
	local pubkey
	pubkey="$(cat "${SSH_KEY_PATH}.pub")"
	local askpass
	askpass="$(mktemp -t webos-root-askpass.XXXXXX)"
	chmod 700 "$askpass"
	printf '#!/bin/sh\nprintf "%%s" "%s"\n' "$ROOT_PASSWORD" >"$askpass"
	DISPLAY=:0 SSH_ASKPASS="$askpass" SSH_ASKPASS_REQUIRE=force \
		ssh -o StrictHostKeyChecking=no \
			-o PreferredAuthentications=password \
			-o PubkeyAuthentication=no \
			-p 22 "root@${TV_IP}" \
			"mkdir -p /home/root/.ssh && chmod 700 /home/root/.ssh && touch /home/root/.ssh/authorized_keys && chmod 600 /home/root/.ssh/authorized_keys && grep -qF '$pubkey' /home/root/.ssh/authorized_keys || echo '$pubkey' >> /home/root/.ssh/authorized_keys"
	rm -f "$askpass"

	ssh -o BatchMode=yes -o ConnectTimeout=5 -i "$SSH_KEY_PATH" \
		-o StrictHostKeyChecking=no "root@${TV_IP}" "true" \
		|| die "Could not authenticate to root@${TV_IP} with $SSH_KEY_PATH"
	printf "  %s\n" "$(c_grn "SSH key installed on TV")"
}

setup_ssh() {
	section "SSH setup"
	printf "  TV: %s\n" "$TV_IP"
	printf "  Key: %s\n" "$SSH_KEY_PATH"

	ensure_deploy_key
	install_key_on_tv

	section "Verifying SSH connection"
	wait_for_ssh || die "SSH to root@${TV_IP} failed after key setup"
	ssh_tv "uname -a" || die "SSH exec to root@${TV_IP} failed"
	printf "  %s\n" "$(c_grn "SSH connection verified")"

	detect_luna_send_style
	printf "  %s\n" "Luna CLI: ${LUNA_SEND_STYLE} (webOS 6 = standard, rooted newer = pub)"

	[[ "$SETUP_DEVICE" == "1" ]] && setup_ares_device
}

setup_ares_device() {
	section "Configuring ares device ($DEVICE)"
	ares_cmd ares-setup-device --remove "$DEVICE" >/dev/null 2>&1 || true
	ares_cmd ares-setup-device --add "$DEVICE" \
		--info "username=root" \
		--info "privatekey=$SSH_KEY" \
		--info "host=$TV_IP" \
		--info "port=22" \
		--info "description=Rooted LG TV"
	ares_cmd ares-setup-device --default "$DEVICE" >/dev/null
	printf "  %s\n" "$(c_grn "Device $DEVICE -> root@${TV_IP}:22")"
}

build_and_package() {
	section "Building app"
	cd "$SCRIPT_DIR"
	npm install
	npm run pack
}

install_ipk_ssh() {
	local ipk="$1" base remote_dir remote_ipk payload version install_cmd log_file
	local max_wait=180 elapsed=0 output appinfo_path icon_path expected_icon_bytes
	base="$(basename "$ipk")"
	remote_dir="/media/developer/temp"
	remote_ipk="${remote_dir}/${base}"
	log_file="/tmp/lounge-install-${base}.log"
	appinfo_path="/media/developer/apps/usr/palm/applications/${APP_ID}/appinfo.json"
	icon_path="/media/developer/apps/usr/palm/applications/${APP_ID}/assets/icon.png"
	version="$(read_version)"
	# Byte size of the icon we are shipping — used to confirm the install actually
	# overwrote assets (same-version reinstalls can leave old files in place).
	expected_icon_bytes="$(wc -c <"$SCRIPT_DIR/dist/assets/icon.png" | tr -d ' ')"

	luna_oneshot_tv 'luna://com.webos.applicationManager/closeByAppId' "{\"id\":\"${APP_ID}\"}"
	# closeByAppId only *suspends* the web app; webOS resumes the same in-memory
	# page (old code) on the next launch, so a plain reinstall never loads new
	# JS/HTML. Hard-kill the WAM process for this app to force a true cold start.
	ssh_tv "pkill -f '${APP_ID}' 2>/dev/null; true" >/dev/null 2>&1 || true

	ssh_tv "rm -rf '${remote_dir}' && mkdir -p '${remote_dir}' && chmod 777 '${remote_dir}'"
	scp -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no \
		-i "$SSH_KEY_PATH" "$ipk" "root@${TV_IP}:${remote_ipk}"

	ssh_tv "restart appinstalld" >/dev/null 2>&1 || true

	payload="$(printf '{"id":"com.ares.defaultName","ipkUrl":"%s","subscribe":true}' "$remote_ipk")"
	install_cmd="$(luna_subscribe_tv_cmd 'luna://com.webos.appInstallService/dev/install' "$payload" 25 60000)"
	# dev/install streams progress replies; a foreground ssh -tt subscribe can hang
	# after "installed" while luna-send-pub waits for more replies. Run on-TV in the
	# background and poll the install log. Do NOT treat an already-matching
	# appinfo version as success — same-version reinstalls used to short-circuit
	# here and leave old icons/assets on disk while the script reported OK.
	printf "  %s\n" "Installing on TV (via appinstalld)..."
	ssh_tv "rm -f '${log_file}'; nohup ${install_cmd} > '${log_file}' 2>&1 </dev/null &"

	while [[ "$elapsed" -lt "$max_wait" ]]; do
		if ssh_tv "test -f '${log_file}' && grep -q '\"state\":\"installed\"' '${log_file}'"; then
			break
		fi
		if ssh_tv "test -f '${log_file}' && grep -q '\"state\":\"failed\"' '${log_file}'"; then
			output="$(ssh_tv "cat '${log_file}'" 2>/dev/null || true)"
			die "SSH install failed for ${APP_ID} ${version}. Output: ${output}"
		fi
		printf "  %s\n" "Waiting for install... (${elapsed}s)"
		sleep 2
		elapsed=$((elapsed + 2))
	done

	if [[ "$elapsed" -ge "$max_wait" ]]; then
		output="$(ssh_tv "cat '${log_file}'" 2>/dev/null || true)"
		die "SSH install timed out after ${max_wait}s for ${APP_ID} ${version}. Output: ${output}"
	fi

	ssh_tv "grep -q '\"version\": *\"${version}\"' '${appinfo_path}'" \
		|| die "SSH install did not update ${APP_ID} to version ${version} on the TV"

	# Confirm assets were actually replaced (catches same-version no-op installs).
	if [[ -n "$expected_icon_bytes" && "$expected_icon_bytes" -gt 0 ]]; then
		local remote_icon_bytes
		remote_icon_bytes="$(ssh_tv "wc -c <'${icon_path}' 2>/dev/null" | tr -d '[:space:]' || true)"
		if [[ "$remote_icon_bytes" != "$expected_icon_bytes" ]]; then
			die "Install reported success but icon size is ${remote_icon_bytes:-missing} bytes (expected ${expected_icon_bytes}). Same-version reinstall may have been skipped — bump version.md and retry."
		fi
	fi

	ssh_tv "rm -f '${remote_ipk}' '${log_file}'"
	ssh_tv "/usr/sbin/ls-control scan-services" >/dev/null
}

install_ipk() {
	local version ipk install_js attempt max_attempts delay
	version="$(read_version)"
	ipk="${APP_ID}_${version}_all.ipk"
	[[ -f "$SCRIPT_DIR/dist/$ipk" ]] || die "Missing package: dist/$ipk"
	install_js="$(ares_js ares-install)"

	section "Installing to TV"
	# ares-install's SSH exec channel is flaky against this TV's dropbear (fails
	# with "Unable to exec"); one quick retry catches transient hiccups without
	# burning time on a failure mode that the SSH fallback below handles anyway.
	max_attempts=2
	delay=3
	# Dropbear on the TV can reject rapid back-to-back SSH exec channels.
	sleep 3
	for attempt in $(seq 1 "$max_attempts"); do
		wait_for_ssh || {
			printf "  %s\n" "$(c_ylw "SSH not ready (attempt $attempt/$max_attempts)")"
			sleep "$delay"
			continue
		}
		if node20 "$install_js" -d "$DEVICE" "$SCRIPT_DIR/dist/$ipk"; then
			printf "  %s\n" "$(c_grn "Installed $ipk")"
			return
		fi
		if [[ "$attempt" -lt "$max_attempts" ]]; then
			printf "  %s\n" "$(c_ylw "Install failed (attempt $attempt/$max_attempts), retrying in ${delay}s...")"
			sleep "$delay"
		fi
	done

	section "Installing via SSH fallback"
	install_ipk_ssh "$SCRIPT_DIR/dist/$ipk"
	printf "  %s\n" "$(c_grn "Installed $ipk (via SSH)")"
}

launch_app_ssh() {
	local payload output
	payload="$(printf '{"id":"%s"}' "$APP_ID")"
	output="$(luna_send_tv "$(luna_call_tv_cmd 'luna://com.webos.applicationManager/launch' "$payload")")"
	printf '%s\n' "$output" | grep -q '"returnValue":true' \
		|| die "SSH launch failed for ${APP_ID}. Output: $output"
}

launch_app() {
	local launch_js attempt max_attempts delay
	launch_js="$(ares_js ares-launch)"
	section "Launching app"

	max_attempts=2
	delay=3
	for attempt in $(seq 1 "$max_attempts"); do
		wait_for_ssh || {
			printf "  %s\n" "$(c_ylw "SSH not ready (attempt $attempt/$max_attempts)")"
			sleep "$delay"
			continue
		}
		if node20 "$launch_js" -d "$DEVICE" "$APP_ID"; then
			printf "  %s\n" "$(c_grn "Launched $APP_ID")"
			return
		fi
		if [[ "$attempt" -lt "$max_attempts" ]]; then
			printf "  %s\n" "$(c_ylw "Launch failed (attempt $attempt/$max_attempts), retrying in ${delay}s...")"
			sleep "$delay"
		fi
	done

	section "Launching via SSH fallback"
	launch_app_ssh
	printf "  %s\n" "$(c_grn "Launched $APP_ID (via SSH)")"
}

main() {
	section "Launch Home installer"
	printf "  TV: %s\n" "$TV_IP"
	printf "  Device: %s\n" "$DEVICE"

	command -v npm >/dev/null 2>&1 || die "npm is required"
	[[ -f "$SCRIPT_DIR/node_modules/@webos-tools/cli/bin/ares-package.js" ]] \
		|| command -v ares-package >/dev/null 2>&1 \
		|| die "@webos-tools/cli is required (npm install)"

	setup_ssh

	if [[ "$SSH_ONLY" == "1" ]]; then
		section "Done"
		printf "  %s\n" "$(c_grn "SSH setup complete (SSH_ONLY=1, skipped build/install)")"
		return
	fi

	build_and_package
	install_ipk
	[[ "$LAUNCH" == "1" ]] && launch_app

	section "Done"
	printf "  %s\n" "$(c_grn "Launch Home is on your TV")"
}

main "$@"