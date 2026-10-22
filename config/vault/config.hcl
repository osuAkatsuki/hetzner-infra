# Credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in /etc/vault.d/vault.env,
# which the vault package's systemd unit loads.
storage "s3" {
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
