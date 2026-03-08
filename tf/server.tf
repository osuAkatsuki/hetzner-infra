resource "hcloud_ssh_key" "deploy" {
  name       = "akatsuki-deploy"
  public_key = var.ssh_public_key
}

resource "hcloud_server" "production" {
  name        = "akatsuki-production"
  server_type = "cx53"
  location    = "fsn1"
  image       = "ubuntu-24.04"
  ssh_keys    = [hcloud_ssh_key.deploy.id]

  firewall_ids = [hcloud_firewall.production.id]

  labels = {
    environment = "production"
    project     = "akatsuki"
  }
}

resource "hcloud_firewall" "production" {
  name = "akatsuki-production"

  # SSH - Tailscale only
  rule {
    description = "SSH via Tailscale"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = [var.tailscale_ipv4_range]
  }

  # HTTP - Cloudflare only
  dynamic "rule" {
    for_each = [var.cloudflare_ipv4_ranges]
    content {
      description = "HTTP from Cloudflare"
      direction   = "in"
      protocol    = "tcp"
      port        = "80"
      source_ips  = rule.value
    }
  }

  # HTTPS - Cloudflare only
  dynamic "rule" {
    for_each = [var.cloudflare_ipv4_ranges]
    content {
      description = "HTTPS from Cloudflare"
      direction   = "in"
      protocol    = "tcp"
      port        = "443"
      source_ips  = rule.value
    }
  }

  # Vault UI - Tailscale only
  rule {
    description = "Vault via Tailscale"
    direction   = "in"
    protocol    = "tcp"
    port        = "8200"
    source_ips  = [var.tailscale_ipv4_range]
  }

  # Grafana - Tailscale only
  rule {
    description = "Grafana via Tailscale"
    direction   = "in"
    protocol    = "tcp"
    port        = "3001"
    source_ips  = [var.tailscale_ipv4_range]
  }
}
