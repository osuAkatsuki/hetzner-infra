terraform {
  backend "s3" {
    bucket = "akatsuki-terraform-state"
    key    = "server-infra/terraform.tfstate"
    region = "ca-central-1"

    endpoints = {
      s3 = "https://s3.ca-central-1.wasabisys.com"
    }

    # Wasabi doesn't support these S3 features
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    skip_region_validation      = true
    use_path_style              = true
  }

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.49"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4"
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
