#!/usr/bin/env bash
# Scanner discovery over Bonjour, host-side.
#
# The guest cannot do this: multicast does not cross Lima's vzNAT boundary, so
# `scanimage -L` inside the VM finds nothing. All discovery and name resolution
# happens on macOS and only a plain IPv4 address is handed to the VM.
#
# HP MFPs that speak the proprietary SOAP scan protocol advertise `_scanner._tcp`,
# which is a tighter filter than `_ipp._tcp` -- it is close to exactly the set that
# hpaio can actually drive, and its TXT record carries model and feeder/flatbed
# capability.

MDNS_TYPE="_scanner._tcp"
SEP=$(printf '\001')

# One service instance name per line, de-duplicated across interfaces.
discover_instances() {
  bounded_stream "${1:-4}" dns-sd -B "$MDNS_TYPE" local 2>/dev/null \
    | awk '$2=="Add" {
        name = ""
        for (i = 7; i <= NF; i++) name = name (i > 7 ? " " : "") $i
        if (name != "") print name
      }' \
    | awk '!seen[$0]++'
}

# Raw `dns-sd -L` output for one instance (host:port line, then the TXT line).
_resolve_raw() { bounded_stream "${2:-4}" dns-sd -L "$1" "$MDNS_TYPE" local; }

# Hostname an instance resolves to, trailing dot stripped.
instance_host() {
  _resolve_raw "$1" \
    | sed -n 's/.*can be reached at \([^:]*\):[0-9]*.*/\1/p' \
    | head -1 | sed 's/\.$//'
}

instance_port() {
  _resolve_raw "$1" \
    | sed -n 's/.*can be reached at [^:]*:\([0-9]*\).*/\1/p' | head -1
}

# Pull one key out of the TXT record. Values may contain backslash-escaped spaces
# (ty=HP\ LaserJet\ 200\ ...), so protect those before splitting on whitespace.
instance_txt() {
  local inst="$1" key="$2"
  _resolve_raw "$inst" | grep -F 'txtvers=' | head -1 \
    | sed "s/\\\\ /${SEP}/g" \
    | tr ' ' '\n' \
    | sed -n "s/^${key}=//p" \
    | head -1 \
    | tr "${SEP}" ' '
}

# Resolve a .local hostname to IPv4. dns-sd is preferred because it does not need
# the host to answer ICMP; ping is the fallback.
resolve_ipv4() {
  local host="${1%.}" ip
  ip=$(bounded_stream "${2:-4}" dns-sd -G v4 "$host" \
       | awk '$2=="Add" {
           for (i = 1; i <= NF; i++)
             if ($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) { print $i; exit }
         }' | head -1)
  if [ -z "$ip" ]; then
    ip=$(ping -c1 -t2 "$host" 2>/dev/null \
         | sed -n '1s/.*(\([0-9.]*\)).*/\1/p' | head -1)
  fi
  [ -n "$ip" ] && printf '%s\n' "$ip"
}

is_ipv4() { printf '%s' "$1" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; }
