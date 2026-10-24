output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "storage_blob_endpoint" {
  value = azurerm_storage_account.data.primary_blob_endpoint
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.main.fqdn
}

output "servicebus_namespace" {
  value = "${azurerm_servicebus_namespace.main.name}.${local.is_gov ? "servicebus.usgovcloudapi.net" : "servicebus.windows.net"}"
}

output "workload_identity_client_id" {
  value = azurerm_user_assigned_identity.workload.client_id
}

output "application_insights_connection_string" {
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}

# Drop-in INGEST_* settings for the API container; compare with compose.yaml for Keycloak.
output "api_settings" {
  value = {
    INGEST_OIDC_ISSUER        = local.authority
    INGEST_OIDC_AUDIENCE      = azuread_application.api.client_id
    INGEST_WEB_OIDC_AUTHORITY = local.authority
    INGEST_WEB_OIDC_CLIENT_ID = azuread_application.web.client_id
    INGEST_WEB_OIDC_SCOPE     = "openid profile email api://${azuread_application.api.client_id}/access_as_user"
  }
}
