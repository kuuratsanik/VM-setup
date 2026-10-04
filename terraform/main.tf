terraform {
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "~> 0.8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "libvirt" {
  uri = "qemu:///system"
}

variable "profile_file" {
  default = "../profile.generated.json"
}

variable "ubuntu_image_url" {
  description = "Ubuntu Server LTS minimal cloud image; point to the newest LTS release"
  default     = "https://cloud-images.ubuntu.com/minimal/releases/resolute/release/ubuntu-26.04-minimal-cloudimg-amd64.img"
}

variable "ssh_public_key" {
  type = string
}

variable "gitops_repo_url" {
  description = "Git repo that contains gitops/ (this repo)"
  type        = string
}

variable "gitops_revision" {
  default = "main"
}

locals {
  p     = jsondecode(file(var.profile_file))
  nodes = local.p.nodes

  clusters = toset([for n in local.nodes : n.cluster])
  primary_ip = {
    for c in local.clusters : c => one([for n in local.nodes : n.ip if n.cluster == c && n.primary])
  }

  k3s_exec = {
    for k, n in local.nodes : k => (
      n.role == "agent" ? "agent" :
      !n.ha ? "server" :
      n.primary ? "server --cluster-init" : "server"
    )
  }
  k3s_url = {
    for k, n in local.nodes : k => n.primary ? "" : "https://${local.primary_ip[n.cluster]}:6443"
  }
}

resource "random_password" "k3s_token" {
  for_each = local.clusters
  length   = 48
  special  = false
}

resource "libvirt_network" "k8s" {
  name      = "k8s"
  mode      = "nat"
  domain    = "k8s.local"
  addresses = ["10.10.10.0/24"]
  bridge    = "virbr-k8s"

  dhcp {
    enabled = false
  }

  dns {
    enabled = true
  }
}

resource "libvirt_volume" "base" {
  name   = "ubuntu-lts-base.qcow2"
  source = var.ubuntu_image_url
  format = "qcow2"
}

resource "libvirt_volume" "disk" {
  for_each       = local.nodes
  name           = "${each.key}.qcow2"
  base_volume_id = libvirt_volume.base.id
  size           = each.value.disk_gb * 1024 * 1024 * 1024
}

resource "libvirt_cloudinit_disk" "init" {
  for_each = local.nodes
  name     = "${each.key}-init.iso"

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    name            = each.key
    cluster         = each.value.cluster
    ssh_public_key  = var.ssh_public_key
    token           = random_password.k3s_token[each.value.cluster].result
    k3s_exec        = local.k3s_exec[each.key]
    k3s_url         = local.k3s_url[each.key]
    gitops          = each.value.primary
    gitops_repo_url = var.gitops_repo_url
    gitops_revision = var.gitops_revision
  })

  network_config = yamlencode({
    version = 2
    ethernets = {
      nic0 = {
        match       = { name = "en*" }
        addresses   = ["${each.value.ip}/24"]
        routes      = [{ to = "default", via = "10.10.10.1" }]
        nameservers = { addresses = ["10.10.10.1"] }
      }
    }
  })
}

resource "libvirt_domain" "node" {
  for_each   = local.nodes
  name       = each.key
  vcpu       = each.value.vcpu
  memory     = each.value.ram_gb * 1024
  autostart  = true
  qemu_agent = true
  cloudinit  = libvirt_cloudinit_disk.init[each.key].id

  cpu {
    mode = "host-passthrough"
  }

  disk {
    volume_id = libvirt_volume.disk[each.key].id
  }

  network_interface {
    network_id = libvirt_network.k8s.id
  }

  console {
    type        = "pty"
    target_port = "0"
    target_type = "serial"
  }
}

output "nodes" {
  value = { for k, n in local.nodes : k => n.ip }
}
