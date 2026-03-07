storage "s3" {
  access_key = "${VAULT_S3_ACCESS_KEY}"
  secret_key = "${VAULT_S3_SECRET_KEY}"
  bucket = "akatsuki.pw"
  endpoint = "https://s3.ca-central-1.wasabisys.com"
  region = "ca-central-1"
  path = "vault"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = "true"
  telemetry {
    unauthenticated_metrics_access = true
  }
}

api_addr = "http://0.0.0.0:8200"
cluster_addr = "https://127.0.0.1:8201"
ui = true
