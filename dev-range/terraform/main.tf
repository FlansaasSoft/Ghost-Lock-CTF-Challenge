locals {
  prefix = "gl-dev-${var.deployment_id}"
  nodes = {
    analyst = { image = tostring(var.kali_image_id), size = "s-2vcpu-4gb" }
    victim  = { image = "ubuntu-24-04-x64", size = "s-1vcpu-2gb" }
    c2      = { image = "ubuntu-24-04-x64", size = "s-2vcpu-4gb" }
    goal    = { image = "ubuntu-24-04-x64", size = "s-1vcpu-1gb" }
    router  = { image = "ubuntu-24-04-x64", size = "s-1vcpu-1gb" }
  }
}

# All managed resources are NEW. There are intentionally no resources for the
# existing gateway, relay, VPC, pilot snapshots, or their firewall rules.
resource "digitalocean_vpc" "range" {
  name        = "${local.prefix}-vpc"
  region      = var.region
  ip_range    = var.underlay_cidr
  description = "Isolated Ghost Lock development underlay ${var.deployment_id}"
}

resource "digitalocean_tag" "range" {
  name = local.prefix
}

resource "digitalocean_ssh_key" "range" {
  name       = "${local.prefix}-automation"
  public_key = trimspace(var.ssh_public_key)
}

resource "digitalocean_droplet" "node" {
  for_each          = local.nodes
  name              = "${local.prefix}-${each.key}"
  region            = var.region
  image             = each.value.image
  size              = each.value.size
  vpc_uuid          = digitalocean_vpc.range.id
  ssh_keys          = [digitalocean_ssh_key.range.fingerprint]
  tags              = [digitalocean_tag.range.name]
  backups           = false
  ipv6              = false
  monitoring        = false
  public_networking = true
  resize_disk       = false
  graceful_shutdown = true
  user_data = templatefile(each.key == "analyst" ? "${path.module}/kali-cloud-init.yaml.tftpl" : "${path.module}/cloud-init.yaml.tftpl", {
    deployment_id = var.deployment_id
    role          = each.key
  })
}

resource "digitalocean_project" "range" {
  name        = local.prefix
  description = "Temporary Linux-only Ghost Lock development range ${var.deployment_id}"
  purpose     = "Operational / Developer tooling"
  environment = "Development"
  resources   = [for node in digitalocean_droplet.node : node.urn]
}

# Exact droplet attachments, never tag selectors. This prevents accidental
# attachment to pre-existing resources, even if an operator reuses a tag.
resource "digitalocean_firewall" "node" {
  for_each    = local.nodes
  name        = "${local.prefix}-${each.key}"
  droplet_ids = [digitalocean_droplet.node[each.key].id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_cidrs
  }

  inbound_rule {
    protocol   = "udp"
    port_range = "4789"
    source_addresses = each.key == "router" ? [
      for role, node in digitalocean_droplet.node : "${node.ipv4_address_private}/32" if role != "router"
    ] : ["${digitalocean_droplet.node["router"].ipv4_address_private}/32"]
  }

  # The new router is also the Linux Ansible controller. Its deployment key
  # reaches only the new nodes; no private key is put in the analyst/victim.
  dynamic "inbound_rule" {
    for_each = each.key == "router" ? [] : [1]
    content {
      protocol         = "tcp"
      port_range       = "22"
      source_addresses = ["${digitalocean_droplet.node["router"].ipv4_address_private}/32"]
    }
  }

  dynamic "inbound_rule" {
    for_each = each.key == "analyst" ? [1] : []
    content {
      protocol         = "tcp"
      port_range       = "3389"
      source_addresses = ["${var.relay_public_ip}/32"]
    }
  }

  outbound_rule {
    protocol   = "udp"
    port_range = "4789"
    destination_addresses = each.key == "router" ? [
      for role, node in digitalocean_droplet.node : "${node.ipv4_address_private}/32" if role != "router"
    ] : ["${digitalocean_droplet.node["router"].ipv4_address_private}/32"]
  }

  dynamic "outbound_rule" {
    for_each = each.key == "router" ? [1] : []
    content {
      protocol   = "tcp"
      port_range = "22"
      destination_addresses = [
        for role, node in digitalocean_droplet.node : "${node.ipv4_address_private}/32" if role != "router"
      ]
    }
  }

  # Runtime has no arbitrary Internet or management-network egress. Stateful
  # replies to allowed SSH and RDP connections remain possible.
  dynamic "outbound_rule" {
    for_each = var.bootstrap_egress ? ["tcp", "udp"] : []
    content {
      protocol              = outbound_rule.value
      port_range            = "53"
      destination_addresses = ["0.0.0.0/0"]
    }
  }

  dynamic "outbound_rule" {
    for_each = var.bootstrap_egress ? ["80", "443"] : []
    content {
      protocol              = "tcp"
      port_range            = outbound_rule.value
      destination_addresses = ["0.0.0.0/0"]
    }
  }
}

