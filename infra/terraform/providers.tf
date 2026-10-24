locals {
  is_gov = var.cloud == "usgovernment"
}

provider "azurerm" {
  features {}
  environment     = var.cloud
  subscription_id = var.subscription_id
  # Data-plane calls (containers, lifecycle) use Entra ID, so shared keys can stay disabled.
  storage_use_azuread = true
}

provider "azuread" {
  # azuread names the commercial cloud "global"; azurerm calls it "public".
  environment = local.is_gov ? "usgovernment" : "global"
}
