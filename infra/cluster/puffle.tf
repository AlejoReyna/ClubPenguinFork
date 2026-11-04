variable "puffle_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.puffle_enabled || (var.game_enabled && var.web_enabled)
    error_message = "Puffle needs the game and web enabled."
  }
}

variable "puffle_service_secret" {
  type    = string
  default = "puffle-service"
}

variable "puffle_allowed_players" {
  type    = list(number)
  default = []
}

# Pairing hashes only; OAuth tokens remain on the player's computer.
resource "kubernetes_persistent_volume_claim_v1" "puffle_devices" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "puffle-devices"
    namespace = local.game_namespace
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources { requests = { storage = "64Mi" } }
  }
  wait_until_bound = false
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_deployment_v1" "puffle" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "puffle"
    namespace = local.game_namespace
  }
  spec {
    replicas = 1
    strategy { type = "Recreate" }
    selector { match_labels = { app = "puffle" } }
    template {
      metadata {
        labels = merge(local.game_labels, { app = "puffle" })
        annotations = {
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "9103"
        }
      }
      spec {
        automount_service_account_token = false
        enable_service_links            = false
        image_pull_secrets { name = kubernetes_secret_v1.registry[0].metadata[0].name }
        security_context {
          run_as_non_root        = true
          run_as_user            = 10001
          run_as_group           = 10001
          fs_group               = 10001
          fs_group_change_policy = "OnRootMismatch"
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name  = "puffle"
          image = local.runtime_image
          env {
            name  = "MODE"
            value = "puffle"
          }
          env {
            name  = "PUFFLE_ENABLED"
            value = tostring(var.puffle_enabled)
          }
          env {
            name  = "PUFFLE_SECRET_FILE"
            value = "/etc/puffle/secret"
          }
          port { container_port = 9103 }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 9103
            }
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 9103
            }
            initial_delay_seconds = 10
          }
          resources {
            requests = { cpu = "25m", memory = "48Mi" }
            limits   = { cpu = "250m", memory = "192Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "secret"
            mount_path = "/etc/puffle"
            read_only  = true
          }
          volume_mount {
            name       = "devices"
            mount_path = "/var/lib/puffle"
          }
        }
        volume {
          name = "secret"
          secret {
            secret_name  = var.puffle_service_secret
            optional     = !var.puffle_enabled
            default_mode = "0440"
          }
        }
        volume {
          name = "devices"
          persistent_volume_claim { claim_name = kubernetes_persistent_volume_claim_v1.puffle_devices[0].metadata[0].name }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "puffle" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "puffle"
    namespace = local.game_namespace
  }
  spec {
    selector = { app = "puffle" }
    port {
      port        = 9103
      target_port = 9103
    }
  }
}

resource "kubernetes_network_policy_v1" "puffle" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "puffle"
    namespace = local.game_namespace
  }
  spec {
    pod_selector { match_labels = { app = "puffle" } }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector { match_labels = { app = "web" } }
      }
      from {
        pod_selector { match_labels = { app = "glaciar" } }
      }
      from {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "monitoring-lab" } }
      }
      ports {
        port     = "9103"
        protocol = "TCP"
      }
    }
    # No egress: inference and computer actions run on the player's computer.
  }
}
