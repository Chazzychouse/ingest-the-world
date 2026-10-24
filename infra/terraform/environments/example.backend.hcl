# Copy to <env>.backend.hcl and switch `backend "local"` to `backend "azurerm"` in versions.tf.
# Gov: also set environment = "usgovernment".
resource_group_name  = "rg-tfstate"
storage_account_name = "sttfstateXXXXXX"
container_name       = "tfstate"
key                  = "ingest-dev.tfstate"
use_azuread_auth     = true
