# Cloudflare record IDs of the DNS records created before Terraform managed DNS. Once a record
# is in state, its import block is a no-op.

locals {
  imported_server_record_ids = {
    "a-staging"        = "94ea895cb094fcf6bef4087bef3f0f64"
    "beatmaps-staging" = "fb0485d62add0d6cf479e166e2b7b05c"
    "c-staging"        = "d178df5fc032b3cdb2044e1c12c7f0b4"
    "c4-staging"       = "e386272ac547ebc4de5d32bd3275f265"
    "ce-staging"       = "a30a87846ffa7bc8ae04fa34fde3a79e"
    "grafana-staging"  = "9bc9148a27d6116ac005a2a55c73d716"
    "old-staging"      = "bb4c3f8001f563c6c3091ae9d238ca2f"
    "osu-staging"      = "ee2c56f990530584f8570fab822fc78e"
    "staging"          = "8ab71168ceee962ec0149cbab7fba5ae"
    "users-staging"    = "ce7f30dcbe3540cbcf65c236bc331f3e"
  }

  imported_cname_record_ids = {
    "a"                = "179f0b68cd0e7f97a07175291846cc1f"
    "afterburn"        = "8b51f41e0697927298caa5b21d713dc3"
    "air_conditioning" = "7097f7485cca66490f13b70f7309453b"
    "assets"           = "94b09abead16cf373a0e864a6a405253"
    "b"                = "6ee19f44bf87065363caee9d3c05a7a5"
    "beatmaps"         = "fad0bd13cea5e0fb81469c735ecb612a"
    "c"                = "716ef8ba0d0d3bf5ac99d7b86afa7477"
    "difficulty"       = "60590adccff06bef9cc8cd88cfcb7595"
    "grafana"          = "8341cc2c57d6e5a97349cede596a1863"
    "next"             = "ffcec3c231e30b15f115d9bb36f650c4"
    "old"              = "49009678a2c31101a4b0484428c5ee9e"
    "osu"              = "e5f9fa810f24478ec6212e2e5bd40542"
    "payments"         = "e0d1427279c9900c22b897d7b33efd9c"
    "performance"      = "f8963f0589aab4febc666e249e3b18a3"
    "relax"            = "ac35937a3b1dd16002c34ff979c1c69c"
    "rework"           = "e3e21a2cfcf8d367fb84fffd12881f8d"
    "reworks"          = "660b7bf2540aeb1d28590e866876ca44"
    "s"                = "607a671bcc399aee897f192ffbf904e2"
    "users"            = "3928f2c5460fb3d651895ef4dc747795"
    "vault"            = "4661649de0c6385b1bc5c4a10c33ba8f"
    "www"              = "d37fc100eb9a8a0eec2f25281d95a7b1"
  }

  imported_mx_record_ids = {
    "aspmx.l.google.com"                                                              = "cc99f7278220097b3f5762ec3d8a964c"
    "alt1.aspmx.l.google.com"                                                         = "18809f26ceae1650b15bd34708cf5209"
    "alt2.aspmx.l.google.com"                                                         = "ede7c6ea5f4e989e2f41010a704d8ccc"
    "alt3.aspmx.l.google.com"                                                         = "e053bc92f9485f547e1bab88cd49f63e"
    "alt4.aspmx.l.google.com"                                                         = "bbe86d4aacefd3b9e041971237326ad3"
    "3h5azgn53tixa3a2yxyqkgyethll22hdjl7jj5jshsfw2wpalkhq.mx-verification.google.com" = "cddb658c1a211af6540fc615cae450ca"
  }

  imported_atproto_record_ids = {
    "_atproto"         = "043b6f539964fe6c03ca4235cc2484d3"
    "_atproto.mistral" = "1b0ec73f215ffc46fe6236cc7dacba78"
  }
}

import {
  to = cloudflare_record.apex
  id = "${var.cloudflare_zone_id}/c777c536e462f6e23c88838db09ecc15"
}

import {
  for_each = local.imported_server_record_ids
  to       = cloudflare_record.server[each.key]
  id       = "${var.cloudflare_zone_id}/${each.value}"
}

import {
  for_each = local.imported_cname_record_ids
  to       = cloudflare_record.cname[each.key]
  id       = "${var.cloudflare_zone_id}/${each.value}"
}

import {
  for_each = local.imported_mx_record_ids
  to       = cloudflare_record.mx[each.key]
  id       = "${var.cloudflare_zone_id}/${each.value}"
}

import {
  for_each = local.imported_atproto_record_ids
  to       = cloudflare_record.atproto[each.key]
  id       = "${var.cloudflare_zone_id}/${each.value}"
}

import {
  to = cloudflare_record.travel_cmyui_xyz
  id = "${local.cmyui_xyz_zone_id}/44236b75b14cefadca0de28be5791f48"
}
