#!/usr/bin/env bash
set -euo pipefail

# Domains this container is allowed to reach over HTTPS. Everything else is
# dropped. Add your own (private registry, git host, an API your agent calls).
ALLOWED_DOMAINS=(
  "api.anthropic.com"       # runtime model calls
  "console.anthropic.com"   # OAuth / login
  "claude.ai"               # OAuth / login
  "statsig.anthropic.com"   # telemetry (harmless to allow; remove to be strict)
  "registry.npmjs.org"      # only if you install npm packages at runtime
)

ipset destroy allowed 2>/dev/null || true
ipset create allowed hash:ip

for d in "${ALLOWED_DOMAINS[@]}"; do
  for ip in $(dig +short A "$d" | grep -E '^[0-9.]+$'); do
    ipset add allowed "$ip" 2>/dev/null || true
  done
done

# Flush and default-deny.
iptables -F
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Loopback.
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Return traffic for connections we initiated.
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# DNS — restricted to docker's embedded resolver only, so DNS can't be used as
# an exfil channel to an arbitrary attacker-chosen nameserver.
iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -d 127.0.0.11 -j ACCEPT

# Let n8n (same docker network) reach our HTTP endpoint.
iptables -A INPUT -p tcp --dport 8080 -j ACCEPT

# Let us reach the n8n API on the internal docker network (for n8n-mcp's
# create/update/execute tools). Covers docker's 172.16.0.0/12 range; widen or
# narrow to your n8n container's subnet if you customized networking.
iptables -A OUTPUT -p tcp --dport 5678 -d 172.16.0.0/12 -j ACCEPT

# Outbound HTTPS ONLY to the allowlisted IPs.
iptables -A OUTPUT -p tcp --dport 443 -m set --match-set allowed dst -j ACCEPT

echo "Firewall applied. Allowed egress: ${ALLOWED_DOMAINS[*]}"
