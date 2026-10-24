# Entra-only authentication: no database passwords exist. The workload identity is the
# server administrator; per-role database users come with the M1 migrations.
resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "psql-${local.prefix}-${local.suffix}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "17"
  sku_name                      = var.postgres_sku
  storage_mb                    = var.postgres_storage_mb
  backup_retention_days         = 7
  public_network_access_enabled = true
  tags                          = local.tags

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  lifecycle {
    # Azure picks the zone at creation; don't fight it on later plans.
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "workload" {
  server_name         = azurerm_postgresql_flexible_server.main.name
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = azurerm_user_assigned_identity.workload.principal_id
  principal_name      = azurerm_user_assigned_identity.workload.name
  principal_type      = "ServicePrincipal"
}

# Extensions must be allow-listed before CREATE EXTENSION works on Flexible Server.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "POSTGIS,VECTOR,PG_TRGM,BTREE_GIST"
}

resource "azurerm_postgresql_flexible_server_database" "ingest" {
  name      = "ingest"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}
