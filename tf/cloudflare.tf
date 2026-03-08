# Phase 2: Uncomment after data migration is complete and records are imported.
#
# Import existing records first to avoid duplicates:
#   terraform import cloudflare_record.apex <record_id>
#   terraform import 'cloudflare_record.cname["a"]' <record_id>
#   ... (for each CNAME and MX record)
#
# Get record IDs with:
#   curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
#     "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" | jq '.result[]'

# resource "cloudflare_record" "apex" {
#   zone_id = var.cloudflare_zone_id
#   name    = "akatsuki.gg"
#   type    = "A"
#   value   = hcloud_server.production.ipv4_address
#   proxied = true
#   ttl     = 1
# }
#
# locals {
#   cname_subdomains = [
#     "a",
#     "air_conditioning",
#     "assets",
#     "b",
#     "beatmaps",
#     "c",
#     "difficulty",
#     "old",
#     "osu",
#     "payments",
#     "performance",
#     "relax",
#     "rework",
#     "reworks",
#     "s",
#     "vault",
#     "www",
#   ]
# }
#
# resource "cloudflare_record" "cname" {
#   for_each = toset(local.cname_subdomains)
#
#   zone_id = var.cloudflare_zone_id
#   name    = each.value
#   type    = "CNAME"
#   value   = "akatsuki.gg"
#   proxied = true
#   ttl     = 1
# }
#
# resource "cloudflare_record" "mx_primary" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "aspmx.l.google.com"
#   priority = 1
#   proxied  = false
#   ttl      = 1
# }
#
# resource "cloudflare_record" "mx_alt1" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "alt1.aspmx.l.google.com"
#   priority = 5
#   proxied  = false
#   ttl      = 1
# }
#
# resource "cloudflare_record" "mx_alt2" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "alt2.aspmx.l.google.com"
#   priority = 5
#   proxied  = false
#   ttl      = 1
# }
#
# resource "cloudflare_record" "mx_alt3" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "alt3.aspmx.l.google.com"
#   priority = 10
#   proxied  = false
#   ttl      = 1
# }
#
# resource "cloudflare_record" "mx_alt4" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "alt4.aspmx.l.google.com"
#   priority = 10
#   proxied  = false
#   ttl      = 1
# }
#
# resource "cloudflare_record" "mx_verification" {
#   zone_id  = var.cloudflare_zone_id
#   name     = "akatsuki.gg"
#   type     = "MX"
#   value    = "3h5azgn53tixa3a2yxyqkgyethll22hdjl7jj5jshsfw2wpalkhq.mx-verification.google.com"
#   priority = 15
#   proxied  = false
#   ttl      = 1
# }
