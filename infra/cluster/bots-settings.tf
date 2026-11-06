# New bodies are opt-in. These defaults preserve the running three-bot release.
variable "world_capacity" {
  type    = string
  default = "20"
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.world_capacity))
    error_message = "world_capacity must be a positive integer string."
  }
}

variable "bots_cast_enabled" {
  type    = bool
  default = false
}

variable "bots_dialogue_enabled" {
  description = "Phase 2 room dialogue; independent of simulated/api provider selection."
  type        = bool
  default     = false
  validation {
    condition     = !var.bots_dialogue_enabled || var.bots_cast_enabled
    error_message = "Room dialogue requires the cast catalog."
  }
}

variable "bots_life_enabled" {
  description = "Phase 3 local travel, follow leases and calendar; defaults off."
  type        = bool
  default     = false
  validation {
    condition     = !var.bots_life_enabled || var.bots_dialogue_enabled
    error_message = "Phase 3 needs the room director for occupancy reservations."
  }
}

variable "bots_daily_spend_cap_usd" {
  type    = number
  default = null
  validation {
    condition     = var.bots_daily_spend_cap_usd == null ? true : var.bots_daily_spend_cap_usd > 0
    error_message = "The daily spend cap must be positive (null preserves the service setting)."
  }
}

variable "bots_monthly_spend_cap_usd" {
  type    = number
  default = null
  validation {
    condition     = var.bots_monthly_spend_cap_usd == null ? true : var.bots_monthly_spend_cap_usd > 0
    error_message = "The monthly spend cap must be positive (null preserves the service setting)."
  }
}

variable "bots_max_concurrent" {
  type    = number
  default = null
  validation {
    condition     = var.bots_max_concurrent == null ? true : (var.bots_max_concurrent >= 1 && floor(var.bots_max_concurrent) == var.bots_max_concurrent)
    error_message = "Concurrency must be a positive integer."
  }
}

variable "bots_memory_limit" {
  type    = string
  default = "192Mi"
}

variable "bots_cpu_limit" {
  type    = string
  default = "250m"
}

variable "bots_timezone" {
  description = "Pending owner choice; UTC until the island time zone is confirmed."
  type        = string
  default     = "UTC"
}

variable "bots_blocklist_config_map" {
  description = "Optional external non-secret ConfigMap with blocklist.txt exported from chat_filter_rule."
  type        = string
  default     = null
}

resource "kubernetes_config_map_v1" "bots_cast" {
  count = var.bots_enabled && var.bots_cast_enabled ? 1 : 0
  metadata {
    name      = "bots-cast"
    namespace = local.game_namespace
  }
  data = { for name in fileset("${path.module}/../../containers/bots/cast", "*.json") :
    name => file("${path.module}/../../containers/bots/cast/${name}")
  }
}
