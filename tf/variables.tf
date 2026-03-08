variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token"
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for akatsuki.gg"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key content for server access"
  type        = string
}

# Cloudflare IPv4 ranges for firewall rules
# https://www.cloudflare.com/ips/
variable "cloudflare_ipv4_ranges" {
  type = list(string)
  default = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
  ]
}

# Tailscale CGNAT range for SSH access
variable "tailscale_ipv4_range" {
  type    = string
  default = "100.64.0.0/10"
}
