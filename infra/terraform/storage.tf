# Flat namespace on purpose: hierarchical namespace (ADLS Gen2) is irreversible and drops
# blob versioning, change feed and point-in-time restore (docs/research/02 §3).
resource "azurerm_storage_account" "data" {
  name                            = local.storage_name
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  is_hns_enabled                  = false
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags

  blob_properties {
    versioning_enabled  = true
    change_feed_enabled = true

    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }

    # Browsers upload blocks straight to Blob with a user-delegation SAS.
    cors_rule {
      allowed_origins    = var.web_origins
      allowed_methods    = ["GET", "HEAD", "PUT", "OPTIONS"]
      allowed_headers    = ["*"]
      exposed_headers    = ["ETag", "x-ms-request-id", "x-ms-version", "Content-Length"]
      max_age_in_seconds = 3600
    }
  }
}

locals {
  containers = toset(["landing", "raw", "derived", "quarantine"])
}

resource "azurerm_storage_container" "data" {
  for_each              = local.containers
  name                  = each.key
  storage_account_id    = azurerm_storage_account.data.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "data" {
  storage_account_id = azurerm_storage_account.data.id

  # Abandoned or already-promoted uploads. Uncommitted blocks expire on their own after 7 days.
  rule {
    name    = "landing-cleanup"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["landing/"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 7
      }
    }
  }

  # Account-wide default. Per-collection policies (D4) are applied by the platform with
  # blob index tags in M5.
  rule {
    name    = "raw-tiering"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["raw/"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = var.raw_tiering.cool_after_days
        tier_to_cold_after_days_since_modification_greater_than    = var.raw_tiering.cold_after_days
        tier_to_archive_after_days_since_modification_greater_than = var.raw_tiering.archive_after_days
      }
      version {
        change_tier_to_cool_after_days_since_creation = var.raw_tiering.cool_after_days
      }
    }
  }
}

resource "azurerm_role_assignment" "workload_blob" {
  for_each = toset([
    "Storage Blob Data Contributor",
    # Signs user-delegation SAS for direct browser uploads and downloads.
    "Storage Blob Delegator",
  ])
  scope                = azurerm_storage_account.data.id
  role_definition_name = each.key
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}
