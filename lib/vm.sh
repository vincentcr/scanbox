#!/usr/bin/env bash
# The VM as a runtime resource: created on demand, started on demand, stopped when
# idle. Nothing here is install-time -- lima itself is a prerequisite the caller
# checks for and reports, never installs.

VM_NAME="${SCANBOX_VM:-scanbox}"
GUEST_LIB="/usr/local/lib/scanbox"

require_lima() {
  command -v limactl >/dev/null 2>&1 || die \
"lima is not installed. It is a prerequisite, not something this tool installs:

    brew install lima"
}

VM_LOG="${SCANBOX_STATE_DIR:-$HOME/.local/state/scanbox}/lima.log"

# limactl narrates its whole boot to stderr. Keep it in a log and surface it only
# when something actually fails, so normal use stays quiet.
lima_quiet() {
  mkdir -p "$(dirname "$VM_LOG")"
  if ! "$@" >>"$VM_LOG" 2>&1; then
    say "lima failed; last lines of $VM_LOG:"
    tail -15 "$VM_LOG" >&2
    return 1
  fi
}

vm_exists() { limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$VM_NAME"; }
vm_status() { limactl list "$VM_NAME" --format '{{.Status}}' 2>/dev/null; }

# lima's cached status lies after a lid-close or reboot, so ask the guest directly.
vm_responsive() { bounded 15 limactl shell "$VM_NAME" true >/dev/null 2>&1; }

vm_is_provisioned() {
  bounded 20 limactl shell "$VM_NAME" test \
    -x "$GUEST_LIB/autofit.sh" >/dev/null 2>&1 || return 1
  # Any scan backend will do. HP ships bb_soap / bb_soapht / bb_marvell / bb_escl
  # and the installer lays down all of them; which one a given model needs varies,
  # so testing for one specific file would wrongly fail on other printers.
  bounded 20 limactl shell "$VM_NAME" bash -c \
    'for f in /usr/share/hplip/scan/plugins/bb_*.so; do [ -e "$f" ] && exit 0; done; exit 1' \
    >/dev/null 2>&1
}

# Scripts are piped in over stdin rather than read from a mount, so the VM has no
# dependency on where this repo happens to live.
vm_provision() {
  say "provisioning the VM (installs HPLIP and HP's scan plugin; a few minutes)"
  lima_quiet limactl shell "$VM_NAME" sudo bash -s < "$ROOT/provision/10-packages.sh" \
    || die "package installation failed"
  lima_quiet limactl shell "$VM_NAME" sudo bash -s < "$ROOT/provision/20-plugin.sh" \
    || die "HPLIP plugin installation failed"
  vm_sync_lib
  say "provisioning complete"
}

# Push the guest-side library every run. It is one small file over an existing SSH
# connection, and it removes a nasty failure mode: an edited autofit.sh on the host
# with a stale copy in the VM produces wrong page sizes and no error at all.
vm_sync_lib() {
  limactl shell "$VM_NAME" sudo install -d "$GUEST_LIB" >/dev/null 2>&1
  limactl shell "$VM_NAME" sudo tee "$GUEST_LIB/autofit.sh" \
    < "$ROOT/lib/autofit.sh" >/dev/null 2>&1
  limactl shell "$VM_NAME" sudo chmod +x "$GUEST_LIB/autofit.sh" >/dev/null 2>&1
}

vm_ensure() {
  require_lima
  if ! vm_exists; then
    say "no scanbox VM yet -- creating it (first run downloads ~400MB; several minutes)"
    lima_quiet limactl start --name="$VM_NAME" --tty=false "$ROOT/scanbox.yaml" \
      || die "could not create the VM"
    vm_provision
    return
  fi

  if [ "$(vm_status)" = "Running" ]; then
    if ! vm_responsive; then
      say "VM claims to be running but is not responding (stale after sleep); restarting"
      limactl stop -f "$VM_NAME" >/dev/null 2>&1 || true
      lima_quiet limactl start "$VM_NAME" --tty=false || die "could not restart the VM"
    fi
  else
    spinner_start "starting the scanbox VM (~26s)"
    lima_quiet limactl start "$VM_NAME" --tty=false || die "could not start the VM"
    spinner_stop
  fi

  if vm_is_provisioned; then
    vm_sync_lib
  else
    vm_provision
  fi
}

vm_stop() {
  vm_exists || return 0
  [ "$(vm_status)" = "Running" ] || return 0
  limactl stop "$VM_NAME" >/dev/null 2>&1 || limactl stop -f "$VM_NAME" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Idle shutdown.
#
# Deliberately not a launchd agent: a permanently-registered background job is the
# exact thing we removed AirSane to avoid. `scanner` is the only thing that ever
# starts the VM, so it also owns stopping it -- one detached timer, guarded by a
# pidfile, that re-reads the last-used timestamp each tick instead of respawning.
# ---------------------------------------------------------------------------

TIMER_PID_FILE_NAME="idle-timer.pid"

timer_running() {
  local f="$STATE_DIR/$TIMER_PID_FILE_NAME" pid
  [ -f "$f" ] || return 1
  pid=$(cat "$f" 2>/dev/null || echo "")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

idle_timer_arm() {
  local keep_alive_min="$1"
  mark_used
  timer_running && return 0
  mkdir -p "$STATE_DIR"
  # nohup, not setsid -- macOS has no setsid. Detaches from the terminal so the
  # timer outlives the shell that started the scan.
  nohup "$ROOT/bin/scanbox" __idle-timer "$keep_alive_min" \
    >"$STATE_DIR/idle-timer.log" 2>&1 &
  echo $! > "$STATE_DIR/$TIMER_PID_FILE_NAME"
}

# Internal: the detached loop itself.
idle_timer_run() {
  local keep_alive_min="$1"
  local limit=$((keep_alive_min * 60))
  local tick=60
  while :; do
    sleep "$tick"
    vm_exists || break
    [ "$(vm_status)" = "Running" ] || break
    lock_held && continue                     # never stop mid-scan
    if [ "$(seconds_since_use)" -ge "$limit" ]; then
      printf '%s stopping idle VM after %s min\n' "$(date '+%F %T')" "$keep_alive_min"
      vm_stop
      break
    fi
  done
  rm -f "$STATE_DIR/$TIMER_PID_FILE_NAME"
}
