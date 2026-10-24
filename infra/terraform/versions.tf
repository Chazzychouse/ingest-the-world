terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.11"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.10"
    }
  }

  # State backend is supplied at init time so one module serves both clouds:
  #   terraform init -backend-config=environments/<env>.backend.hcl
  # Without it (CI validate, local experiments) state stays local.
  backend "local" {}
}
