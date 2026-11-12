# Credentials are external Secrets, never Secret data sources or Terraform values.
variable "bots_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.bots_enabled || var.game_enabled
    error_message = "Enable the game before enabling bots."
  }
}

variable "bots_image_tag" {
  type    = string
  default = null
  validation {
    condition     = !var.bots_enabled || (var.bots_image_tag != null && var.bots_image_tag != "latest" && can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.bots_image_tag)))
    error_message = "Enabled bots require an explicit image tag; latest is not a release."
  }
}

variable "bots_provider_mode" {
  type    = string
  default = "simulated"
  validation {
    condition     = contains(["simulated", "api"], var.bots_provider_mode)
    error_message = "Choose simulated or api. API mode can spend Moonshot balance."
  }
}

variable "bots_replicas" {
  type    = number
  default = 1
  validation {
    condition     = contains([0, 1], var.bots_replicas)
    error_message = "Use zero to stop bots while retaining the budget and dialog PVCs, or one to run them."
  }
}

variable "bots_credentials_secret" {
  type    = string
  default = "bot-credentials"
}

variable "bots_moonshot_secret" {
  type    = string
  default = "moonshot-api"
}

variable "bots_dialog_salt_secret" {
  description = "External Secret (key salt) for the dialog-log player hash. Created by hand; the value never enters Terraform."
  type        = string
  default     = "bots-dialog-salt"
}

resource "kubernetes_persistent_volume_claim_v1" "bots_budget" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-budget"
    namespace = local.game_namespace
    labels    = local.game_labels
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources { requests = { storage = "64Mi" } }
  }
  wait_until_bound = false
  lifecycle { prevent_destroy = true }
}

# Dialog log only. Separate from bots-budget so a full disk or a bad mount
# cannot fail the spend ledger closed. No prevent_destroy: removing this PVC
# deletes the log. It is not part of the Azure backup.
resource "kubernetes_persistent_volume_claim_v1" "bots_dialog" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-dialog"
    namespace = local.game_namespace
    labels    = local.game_labels
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources { requests = { storage = "1Gi" } }
  }
  wait_until_bound = false
}

# Kill switch. Default off. Do not hash this into a pod annotation: the
# process re-reads the file, and a checksum would roll the pod on every toggle.
# Terraform only sets the initial value: the operator flips it with kubectl
# patch, and a deploy must not silently turn the log (and the public feed) off.
resource "kubernetes_config_map_v1" "bots_dialog_log" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-dialog-log"
    namespace = local.game_namespace
    labels    = local.game_labels
  }
  data = {
    enabled = "false"
  }
  lifecycle {
    ignore_changes = [data]
  }
}

resource "kubernetes_deployment_v1" "bots" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots"
    namespace = local.game_namespace
    labels    = local.game_labels
  }
  spec {
    # One owner per account: rolling surge would log in duplicate sessions.
    replicas = var.bots_replicas
    strategy { type = "Recreate" }
    selector { match_labels = { app = "bots" } }
    template {
      metadata {
        labels = merge(local.game_labels, { app = "bots" })
        annotations = merge({
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "9102"
          }, var.bots_cast_enabled ? {
          "checksum/cast" = sha256(jsonencode(kubernetes_config_map_v1.bots_cast[0].data))
        } : {})
      }
      spec {
        automount_service_account_token  = false
        enable_service_links             = false
        termination_grace_period_seconds = 15
        image_pull_secrets { name = kubernetes_secret_v1.registry[0].metadata[0].name }
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          fs_group        = 10001
          # Avoid recursively widening private files (budget ledger, dialog log)
          # when the volume root already has the expected group and mode.
          fs_group_change_policy = "OnRootMismatch"
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name  = "bots"
          image = "${local.registry}/clubpenguin/bots:${var.bots_image_tag}"
          port {
            name           = "metrics"
            container_port = 9102
          }
          env {
            name  = "BOTS_PROVIDER_MODE"
            value = var.bots_provider_mode
          }
          dynamic "env" {
            for_each = merge(
              var.bots_cast_enabled ? { BOTS_CAST_DIR = "/etc/bots-cast", BOTS_TZ = var.bots_timezone } : {},
              var.bots_dialogue_enabled ? { BOTS_DIALOGUE_ENABLED = "true" } : {},
              var.bots_life_enabled ? { BOTS_LIFE_ENABLED = "true" } : {},
              var.bots_recall_enabled ? { BOTS_RECALL_ENABLED = "true" } : {},
              var.bots_daily_spend_cap_usd == null ? {} : { BOTS_DAILY_SPEND_CAP_USD = tostring(var.bots_daily_spend_cap_usd) },
              var.bots_monthly_spend_cap_usd == null ? {} : { BOTS_MONTHLY_SPEND_CAP_USD = tostring(var.bots_monthly_spend_cap_usd) },
              var.bots_max_concurrent == null ? {} : { BOTS_MAX_CONCURRENT = tostring(var.bots_max_concurrent) },
              var.bots_blocklist_config_map == null ? {} : { BOTS_BLOCKLIST_FILE = "/etc/bots-filter/blocklist.txt" }
            )
            content {
              name  = env.key
              value = env.value
            }
          }
          # Salt is not hot-reloaded (it is an env var). The switch file is.
          # optional so a missing Secret does not stop the bots; player is then null.
          env {
            name = "BOTS_DIALOG_LOG_SALT"
            value_from {
              secret_key_ref {
                name     = var.bots_dialog_salt_secret
                key      = "salt"
                optional = true
              }
            }
          }
          env {
            name  = "BOTS_DIALOG_LOG_DIR"
            value = "/var/lib/bots-dialog"
          }
          env {
            name  = "BOTS_DIALOG_LOG_SWITCH"
            value = "/etc/bots-dialog/enabled"
          }
          dynamic "env" {
            for_each = var.bots_provider_mode == "api" ? [1] : []
            content {
              name = "MOONSHOT_API_KEY"
              value_from {
                secret_key_ref {
                  name = var.bots_moonshot_secret
                  key  = "MOONSHOT_API_KEY"
                }
              }
            }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          resources {
            requests = { cpu = "25m", memory = "48Mi" }
            limits   = { cpu = var.bots_cpu_limit, memory = var.bots_memory_limit }
          }
          volume_mount {
            name       = "credentials"
            mount_path = "/etc/bots"
            read_only  = true
          }
          volume_mount {
            name       = "budget"
            mount_path = "/var/lib/bots"
          }
          volume_mount {
            name       = "dialog-log"
            mount_path = "/var/lib/bots-dialog"
          }
          volume_mount {
            name       = "dialog-switch"
            mount_path = "/etc/bots-dialog"
            read_only  = true
          }
          dynamic "volume_mount" {
            for_each = var.bots_cast_enabled ? [1] : []
            content {
              name       = "cast"
              mount_path = "/etc/bots-cast"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = var.bots_blocklist_config_map == null ? [] : [1]
            content {
              name       = "filter"
              mount_path = "/etc/bots-filter"
              read_only  = true
            }
          }
          readiness_probe {
            http_get {
              path = "/readyz"
              port = 9102
            }
            period_seconds = 5
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 9102
            }
            initial_delay_seconds = 10
            period_seconds        = 15
          }
        }
        volume {
          name = "credentials"
          secret {
            secret_name  = var.bots_credentials_secret
            default_mode = "0440"
          }
        }
        volume {
          name = "budget"
          persistent_volume_claim { claim_name = kubernetes_persistent_volume_claim_v1.bots_budget[0].metadata[0].name }
        }
        volume {
          name = "dialog-log"
          persistent_volume_claim { claim_name = kubernetes_persistent_volume_claim_v1.bots_dialog[0].metadata[0].name }
        }
        volume {
          name = "dialog-switch"
          config_map { name = kubernetes_config_map_v1.bots_dialog_log[0].metadata[0].name }
        }
        dynamic "volume" {
          for_each = var.bots_cast_enabled ? [1] : []
          content {
            name = "cast"
            config_map { name = kubernetes_config_map_v1.bots_cast[0].metadata[0].name }
          }
        }
        dynamic "volume" {
          for_each = var.bots_blocklist_config_map == null ? [] : [1]
          content {
            name = "filter"
            config_map { name = var.bots_blocklist_config_map }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_deployment_v1.runtime]
}

resource "kubernetes_service_v1" "bots_metrics" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-metrics"
    namespace = local.game_namespace
  }
  spec {
    selector = { app = "bots" }
    port {
      name        = "metrics"
      port        = 9102
      target_port = 9102
    }
  }
}

resource "kubernetes_network_policy_v1" "bots_metrics" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-metrics"
    namespace = local.game_namespace
  }
  spec {
    pod_selector { match_labels = { app = "bots" } }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "monitoring-lab" } }
      }
      ports {
        port     = "9102"
        protocol = "TCP"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "bots_egress" {
  count = var.bots_enabled ? 1 : 0
  metadata {
    name      = "bots-egress"
    namespace = local.game_namespace
  }
  spec {
    pod_selector { match_labels = { app = "bots" } }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
        pod_selector { match_labels = { "k8s-app" = "kube-dns" } }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }
    egress {
      to {
        pod_selector { match_labels = { app = "login" } }
      }
      ports {
        port     = "6112"
        protocol = "TCP"
      }
    }
    egress {
      to {
        pod_selector { match_labels = { app = "glaciar" } }
      }
      ports {
        port     = "9876"
        protocol = "TCP"
      }
    }
    dynamic "egress" {
      for_each = var.bots_provider_mode == "api" ? [1] : []
      content {
        # NetworkPolicy cannot restrict by FQDN. This permits public HTTPS,
        # not just Moonshot; private/LAN/link-local destinations are excluded.
        to {
          ip_block {
            cidr   = "0.0.0.0/0"
            except = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8"]
          }
        }
        ports {
          port     = "443"
          protocol = "TCP"
        }
      }
    }
  }
}
