resource "azurerm_servicebus_namespace" "main" {
  name                          = "sb-${local.prefix}-${local.suffix}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = "Standard"
  local_auth_enabled            = false
  public_network_access_enabled = true
  minimum_tls_version           = "1.2"
  tags                          = local.tags
}

resource "azurerm_servicebus_queue" "ingest" {
  name         = "ingest"
  namespace_id = azurerm_servicebus_namespace.main.id

  # Long enough for header-only extraction; long jobs renew the lock.
  lock_duration                        = "PT5M"
  max_delivery_count                   = 5
  dead_lettering_on_message_expiration = true
  default_message_ttl                  = "P7D"
}

resource "azurerm_role_assignment" "workload_servicebus" {
  for_each             = toset(["Azure Service Bus Data Receiver", "Azure Service Bus Data Sender"])
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = each.key
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}

# BlobCreated on `landing/` -> Service Bus `ingest`. Event Grid delivers at least once and
# out of order, so workers are idempotent on (blob URL, eTag).
resource "azurerm_eventgrid_system_topic" "storage" {
  name                = "egst-${local.prefix}-storage"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  source_resource_id  = azurerm_storage_account.data.id
  topic_type          = "Microsoft.Storage.StorageAccounts"
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "eventgrid_to_servicebus" {
  scope                = azurerm_servicebus_queue.ingest.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_eventgrid_system_topic.storage.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_eventgrid_system_topic_event_subscription" "landing_to_ingest" {
  name                = "landing-blob-created"
  system_topic        = azurerm_eventgrid_system_topic.storage.name
  resource_group_name = azurerm_resource_group.main.name

  included_event_types = ["Microsoft.Storage.BlobCreated"]
  subject_filter {
    subject_begins_with = "/blobServices/default/containers/landing/"
  }
  # Only completed writes; Put Block alone is not a finished blob.
  advanced_filter {
    string_in {
      key    = "data.api"
      values = ["PutBlockList", "PutBlob", "CopyBlob", "FlushWithClose"]
    }
  }

  delivery_identity {
    type = "SystemAssigned"
  }
  service_bus_queue_id = azurerm_servicebus_queue.ingest.id

  depends_on = [azurerm_role_assignment.eventgrid_to_servicebus]
}
