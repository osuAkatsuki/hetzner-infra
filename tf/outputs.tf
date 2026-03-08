output "server_ip" {
  description = "Public IPv4 address of the production server"
  value       = hcloud_server.production.ipv4_address
}

output "server_status" {
  description = "Server status"
  value       = hcloud_server.production.status
}
