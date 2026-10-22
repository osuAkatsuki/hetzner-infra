locals {
  cmyui_xyz_zone_id = "3d498eac702151a0b0dbd5b4509f97be"

  # akatsuki.gg subdomains with their own A record pointing at the server
  server_subdomains = toset([
    "a-staging",
    "beatmaps-staging",
    "c-staging",
    "c4-staging",
    "ce-staging",
    "grafana-staging",
    "old-staging",
    "osu-staging",
    "staging",
    "users-staging",
  ])

  # akatsuki.gg subdomains that CNAME to the apex
  cname_subdomains = toset([
    "a",
    "afterburn",
    "air_conditioning",
    "assets",
    "b",
    "beatmaps",
    "c",
    "difficulty",
    "grafana",
    "next",
    "old",
    "osu",
    "payments",
    "performance",
    "relax",
    "rework",
    "reworks",
    "s",
    "users",
    "vault",
    "www",
  ])

  # Google Workspace mail servers and their priorities
  mx_records = {
    "aspmx.l.google.com"                                                              = 1
    "alt1.aspmx.l.google.com"                                                         = 5
    "alt2.aspmx.l.google.com"                                                         = 5
    "alt3.aspmx.l.google.com"                                                         = 10
    "alt4.aspmx.l.google.com"                                                         = 10
    "3h5azgn53tixa3a2yxyqkgyethll22hdjl7jj5jshsfw2wpalkhq.mx-verification.google.com" = 15
  }

  # Bluesky handle verification
  atproto_records = {
    "_atproto.akatsuki.gg"         = "did=did:plc:dgkcb7osp25n3rmnhm4goscz"
    "_atproto.mistral.akatsuki.gg" = "did=did:plc:qjrg33jx6yesuihpvkn3bilj"
  }
}

# Proxied records keep the server's address behind Cloudflare

resource "cloudflare_record" "apex" {
  zone_id = var.cloudflare_zone_id
  name    = "akatsuki.gg"
  type    = "A"
  content = hcloud_server.production.ipv4_address
  proxied = true
  ttl     = 1
}

resource "cloudflare_record" "server" {
  for_each = local.server_subdomains

  zone_id = var.cloudflare_zone_id
  name    = "${each.value}.akatsuki.gg"
  type    = "A"
  content = hcloud_server.production.ipv4_address
  proxied = true
  ttl     = 1
}

resource "cloudflare_record" "cname" {
  for_each = local.cname_subdomains

  zone_id = var.cloudflare_zone_id
  name    = "${each.value}.akatsuki.gg"
  type    = "CNAME"
  content = "akatsuki.gg"
  proxied = true
  ttl     = 1
}

resource "cloudflare_record" "mx" {
  for_each = local.mx_records

  zone_id  = var.cloudflare_zone_id
  name     = "akatsuki.gg"
  type     = "MX"
  content  = each.key
  priority = each.value
  proxied  = false
  ttl      = 1
}

resource "cloudflare_record" "atproto" {
  for_each = local.atproto_records

  zone_id = var.cloudflare_zone_id
  name    = each.key
  type    = "TXT"
  content = "\"${each.value}\""
  proxied = false
  ttl     = 1
}

# Only this cmyui.xyz record points at the server; the rest of the zone is unmanaged
resource "cloudflare_record" "travel_cmyui_xyz" {
  zone_id = local.cmyui_xyz_zone_id
  name    = "travel.cmyui.xyz"
  type    = "A"
  content = hcloud_server.production.ipv4_address
  proxied = true
  ttl     = 1
}
