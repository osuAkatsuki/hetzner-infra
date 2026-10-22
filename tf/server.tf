resource "hcloud_ssh_key" "deploy" {
  name       = "akatsuki-deploy"
  public_key = var.ssh_public_key
}

resource "hcloud_server" "production" {
  name        = "akatsuki-production"
  server_type = "cpx42"
  location    = "nbg1"
  image       = "ubuntu-24.04"
  ssh_keys    = [hcloud_ssh_key.deploy.id]

  firewall_ids = [hcloud_firewall.production.id]

  labels = {
    environment = "production"
    project     = "akatsuki"
  }

  # CI applies this config; never let it delete or replace the production server
  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_firewall" "production" {
  name = "akatsuki-production"

  # SSH - open (key-based auth only, iptables also permits)
  rule {
    description = "SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0", "::/0"]
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
}
