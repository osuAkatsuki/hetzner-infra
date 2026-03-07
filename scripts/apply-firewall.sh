#!/usr/bin/env bash
set -euo pipefail

# Akatsuki Hetzner Firewall Rules
#
# Policy: DROP all inbound by default, allow only:
#   - SSH (22) from anywhere
#   - HTTP (80) and HTTPS (443) from Cloudflare IPs only
#   - Everything on loopback (services communicate via 127.0.0.1)
#   - Established/related connections (return traffic)
#
# All internal ports (MySQL, Redis, RabbitMQ, PostgreSQL, Vault,
# Prometheus, app services) are blocked from external access.

echo "Applying firewall rules..."

iptables -F INPUT
iptables -F OUTPUT
iptables -F FORWARD

iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

# Loopback (all inter-service traffic)
iptables -A INPUT -i lo -j ACCEPT

# Established/related connections
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# SSH
iptables -A INPUT -p tcp --dport 22 -j ACCEPT

# ICMP (ping)
iptables -A INPUT -p icmp -j ACCEPT

# Cloudflare IPv4 ranges — https://www.cloudflare.com/ips/
CF_IPS=(
    "173.245.48.0/20"
    "103.21.244.0/22"
    "103.22.200.0/22"
    "103.31.4.0/22"
    "141.101.64.0/18"
    "108.162.192.0/18"
    "190.93.240.0/20"
    "188.114.96.0/20"
    "197.234.240.0/22"
    "198.41.128.0/17"
    "162.158.0.0/15"
    "104.16.0.0/13"
    "104.24.0.0/14"
    "172.64.0.0/13"
    "131.0.72.0/22"
)

for cidr in "${CF_IPS[@]}"; do
    iptables -A INPUT -p tcp -s "$cidr" --dport 80 -j ACCEPT
    iptables -A INPUT -p tcp -s "$cidr" --dport 443 -j ACCEPT
done

# Log dropped packets (rate-limited)
iptables -A INPUT -m limit --limit 5/min -j LOG \
    --log-prefix "iptables-dropped: " --log-level 4

# Persist rules across reboots
iptables-save > /etc/iptables/rules.v4

echo "Firewall applied and persisted."
