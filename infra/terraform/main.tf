data "azurerm_client_config" "current" {}

locals {
  prefix = "${var.name}-${var.env}"
  # Storage account names are global, 3-24 lowercase alphanumerics; a stable per-subscription
  # suffix keeps them unique without a random provider or extra state.
  suffix       = substr(md5("${data.azurerm_client_config.current.subscription_id}/${local.prefix}"), 0, 6)
  storage_name = substr(replace("${var.name}${var.env}${local.suffix}", "-", ""), 0, 24)
  tags         = merge({ workload = var.name, env = var.env, managed_by = "terraform" }, var.tags)
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.prefix}"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = local.tags
}

# One identity for API and workers. Every service-to-service call authenticates with it;
# there are no connection strings or keys to rotate.
resource "azurerm_user_assigned_identity" "workload" {
  name                = "id-${local.prefix}-workload"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}
