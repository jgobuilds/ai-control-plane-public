#!/usr/bin/env bash
set -euo pipefail

# Egress allowlist for the Gemini CLI's Google endpoints. Adjust if you switch
# to Antigravity or another provider (see Dockerfile ARGs).
ALLOWED_DOMAINS=(
  "generativelanguage.googleapis.com"  # Gemini API
  "cloudcode-pa.googleapis.com"        # free-tier (Google-login) Code Assist path
  "oauth2.googleapis.com"              # token refresh
  "accounts.google.com"               # login
  "www.googleapis.com"                 # misc Google APIs
  "registry.npmjs.org"                 # only if installing packages at runtime
)

ipset destroy allowed 2>/dev/null || true
ipset create allowed hash:ip
for d in "${ALLOWED_DOMAINS[@]}"; do
  for ip in $(dig +short A "$d" | grep -E '^[0-9.]+$'); do
    ipset add allowed "$ip" 2>/dev/null || true
  done
done

iptables -F
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# DNS restricted to docker's embedded resolver (no arbitrary-nameserver exfil).
iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -d 127.0.0.11 -j ACCEPT

# Inbound from the router / n8n on the internal network.
iptables -A INPUT -p tcp --dport 8080 -j ACCEPT

iptables -A OUTPUT -p tcp --dport 443 -m set --match-set allowed dst -j ACCEPT

echo "Firewall applied. Allowed egress: ${ALLOWED_DOMAINS[*]}"
