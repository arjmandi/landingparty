#!/bin/sh
set -eu
case "${WORLD_INTERNAL_IP:-}" in
  172.30.77.2) ;;
  *) echo 'WORLD_INTERNAL_IP must match the reviewed fenced deployment' >&2; exit 1 ;;
esac
# Apply DROP before route-specific acceptance. IPv6 gets no external routes.
iptables -P INPUT DROP
iptables -P OUTPUT DROP
iptables -P FORWARD DROP
iptables -F INPUT
iptables -F OUTPUT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -d "$WORLD_INTERNAL_IP" -p tcp -m multiport --dports 7778,8080,8081 -j ACCEPT
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -P INPUT DROP
  ip6tables -P OUTPUT DROP
  ip6tables -P FORWARD DROP
  ip6tables -F INPUT
  ip6tables -F OUTPUT
  ip6tables -A INPUT -i lo -j ACCEPT
  ip6tables -A OUTPUT -o lo -j ACCEPT
fi
: > /tmp/fence-ready
# The agent runs in a distinct container with ALL capabilities dropped. It never
# executes this privileged initializer, owns these rules, or receives NET_ADMIN.
exec sleep infinity
