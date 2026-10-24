variable "cloud" {
  description = "Azure cloud: \"public\" (commercial) or \"usgovernment\"."
  type        = string
  default     = "public"

  validation {
    condition     = contains(["public", "usgovernment"], var.cloud)
    error_message = "cloud must be \"public\" or \"usgovernment\"."
  }
}

variable "subscription_id" {
  description = "Target subscription. Null falls back to ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "location" {
  description = "Region, e.g. eastus2 (commercial) or usgovvirginia (Gov)."
  type        = string
}

variable "env" {
  description = "Short environment name used in resource names (dev, demo, prod)."
  type        = string
  default     = "dev"
}

variable "name" {
  description = "Workload name used in resource names."
  type        = string
  default     = "ingest"
}

variable "web_origins" {
  description = "SPA origins: OIDC redirect URIs and Blob CORS for direct uploads."
  type        = list(string)
}

variable "postgres_sku" {
  description = "Flexible Server SKU. Burstable for dev; General Purpose or higher for real load."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  type    = number
  default = 32768
}

variable "raw_tiering" {
  description = "Default lifecycle tiering for original files, in days since last modification."
  type = object({
    cool_after_days    = number
    cold_after_days    = number
    archive_after_days = optional(number)
  })
  default = {
    cool_after_days = 30
    cold_after_days = 180
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}
