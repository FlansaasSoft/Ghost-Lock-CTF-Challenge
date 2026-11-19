terraform {
  required_version = "= 1.17.2"
  # range.py supplies an absolute, deployment-local path at init time.
  backend "local" {}
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "= 2.107.0"
    }
  }
}

# DIGITALOCEAN_TOKEN is supplied to the child process only, never a .tfvars file.
provider "digitalocean" {}

