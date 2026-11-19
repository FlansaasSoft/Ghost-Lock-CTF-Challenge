output "deployment" {
  value = {
    deployment_id    = var.deployment_id
    prefix           = local.prefix
    vpc_id           = digitalocean_vpc.range.id
    project_id       = digitalocean_project.range.id
    ssh_key_id       = digitalocean_ssh_key.range.id
    bootstrap_egress = var.bootstrap_egress
    nodes = {
      for role, node in digitalocean_droplet.node : role => {
        id          = tostring(node.id)
        name        = node.name
        public_ip   = node.ipv4_address
        private_ip  = node.ipv4_address_private
        size        = node.size
        created_at  = node.created_at
        firewall_id = digitalocean_firewall.node[role].id
      }
    }
  }
}

