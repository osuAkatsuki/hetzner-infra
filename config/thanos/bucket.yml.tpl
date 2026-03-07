type: S3
config:
  bucket: "akatsuki.pw"
  endpoint: "s3.ca-central-1.wasabisys.com"
  region: "ca-central-1"
  access_key: "${WASABI_ACCESS_KEY}"
  secret_key: "${WASABI_SECRET_KEY}"
  prefix: "observability/thanos"
