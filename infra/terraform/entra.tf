# Entra ID app registrations: the API (resource) and the SPA (public client, PKCE).
# These give Entra tokens the same shape the local Keycloak realm issues: a top-level
# `roles` claim and an `aud` of the API.

locals {
  # Deterministic ids, so plans are stable without a random provider.
  app_roles = {
    Reader          = "Search and view collections they are granted"
    Contributor     = "Upload and edit their own items"
    Curator         = "Edit any item in granted collections"
    Admin           = "Manage collections, grants and markings"
    "Clearance.CUI" = "May read CUI-marked data"
  }
  login_host = local.is_gov ? "login.microsoftonline.us" : "login.microsoftonline.com"
  tenant_id  = data.azurerm_client_config.current.tenant_id
  authority  = "https://${local.login_host}/${local.tenant_id}/v2.0"
}

resource "azuread_application" "api" {
  display_name     = "${local.prefix}-api"
  sign_in_audience = "AzureADMyOrg"

  api {
    # v2 tokens: iss = https://<login host>/<tenant>/v2.0, aud = the API's client id.
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = uuidv5("url", "urn:${local.prefix}:scope:access_as_user")
      value                      = "access_as_user"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Access Ingest the World"
      admin_consent_description  = "Call the Ingest the World API as the signed-in user."
      user_consent_display_name  = "Access Ingest the World"
      user_consent_description   = "Call the Ingest the World API on your behalf."
    }
  }

  dynamic "app_role" {
    for_each = local.app_roles
    content {
      id                   = uuidv5("url", "urn:${local.prefix}:role:${app_role.key}")
      value                = app_role.key
      display_name         = app_role.key
      description          = app_role.value
      allowed_member_types = ["User"]
      enabled              = true
    }
  }
}

# api://<client id> satisfies tenants that block custom identifier URIs on unverified domains.
resource "azuread_application_identifier_uri" "api" {
  application_id = azuread_application.api.id
  identifier_uri = "api://${azuread_application.api.client_id}"
}

resource "azuread_service_principal" "api" {
  client_id                    = azuread_application.api.client_id
  app_role_assignment_required = true
}

resource "azuread_application" "web" {
  display_name     = "${local.prefix}-web"
  sign_in_audience = "AzureADMyOrg"

  single_page_application {
    redirect_uris = [for origin in var.web_origins : "${trimsuffix(origin, "/")}/"]
  }

  required_resource_access {
    resource_app_id = azuread_application.api.client_id
    resource_access {
      id   = uuidv5("url", "urn:${local.prefix}:scope:access_as_user")
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "web" {
  client_id = azuread_application.web.client_id
}
