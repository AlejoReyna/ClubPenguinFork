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

variable "puffle_broker_replicas" {
  type    = number
  default = 1
  validation {
    condition     = contains([0, 1], var.puffle_broker_replicas) && (!var.puffle_enabled || var.puffle_broker_replicas == 1)
    error_message = "Use zero for a legacy rollback, or one; enabled puffle needs one broker."
  }
}

variable "puffle_allowed_players" {
  type    = list(number)
  default = []
}

variable "puffle_web_players" {
  type    = set(number)
  default = []
  validation {
    condition = length(var.puffle_web_players) <= 5 && alltrue([
      for id in var.puffle_web_players : id > 0 && floor(id) == id && id <= 2147483647
    ])
    error_message = "The web pilot permits up to five positive integer player IDs."
  }
}

variable "puffle_spotify_client_id" {
  type    = string
  default = ""
  validation {
    condition     = var.puffle_spotify_client_id == "" || can(regex("^[a-zA-Z0-9]{16,64}$", var.puffle_spotify_client_id))
    error_message = "Provide a valid Spotify public client ID."
  }
}

locals {
  puffle_web_enabled = var.puffle_enabled && length(var.puffle_web_players) > 0 && var.puffle_spotify_client_id != ""
}

# Keep the vault key stable across rollouts and temporary pilot disablement.
resource "random_id" "puffle_host_key" {
  count       = var.web_enabled ? 1 : 0
  byte_length = 32
}

resource "kubernetes_secret_v1" "puffle_host_key" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "puffle-host-key"
    namespace = local.game_namespace
  }
  data = { key = random_id.puffle_host_key[0].hex }
}

# Broker pairings and, when the web pilot is active, encrypted Spotify tokens.
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
    replicas = var.puffle_broker_replicas
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
          dynamic "env" {
            for_each = local.puffle_web_enabled ? {
              PUFFLE_HOST_MODE         = "lab"
              PUFFLE_HOST_KEY_FILE     = "/etc/puffle-host/key"
              PUFFLE_HOST_CONVERSATION = "off"
              PUFFLE_ALLOWED_PLAYERS   = join(",", sort([for id in var.puffle_web_players : tostring(id)]))
              PUFFLE_PUBLIC_ORIGIN     = "https://${var.public_hostname}"
              PUFFLE_SPOTIFY_CLIENT_ID = var.puffle_spotify_client_id
            } : {}
            content {
              name  = env.key
              value = env.value
            }
          }
          port { container_port = 9103 }
          dynamic "port" {
            for_each = local.puffle_web_enabled ? [9104] : []
            content { container_port = port.value }
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = local.puffle_web_enabled ? 9104 : 9103
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
          dynamic "volume_mount" {
            for_each = local.puffle_web_enabled ? [1] : []
            content {
              name       = "host-key"
              mount_path = "/etc/puffle-host"
              read_only  = true
            }
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
        dynamic "volume" {
          for_each = local.puffle_web_enabled ? [1] : []
          content {
            name = "host-key"
            secret {
              secret_name  = kubernetes_secret_v1.puffle_host_key[0].metadata[0].name
              default_mode = "0440"
            }
          }
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
      name        = "broker"
      port        = 9103
      target_port = 9103
    }
    dynamic "port" {
      for_each = local.puffle_web_enabled ? [9104] : []
      content {
        name        = "web-host"
        port        = port.value
        target_port = port.value
      }
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
    dynamic "ingress" {
      for_each = local.puffle_web_enabled ? [1] : []
      content {
        from {
          pod_selector { match_labels = { app = "web" } }
        }
        ports {
          port     = "9104"
          protocol = "TCP"
        }
      }
    }
    dynamic "egress" {
      for_each = local.puffle_web_enabled ? [1] : []
      content {
        to {
          namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
          pod_selector { match_labels = { "k8s-app" = "kube-dns" } }
        }
        dynamic "ports" {
          for_each = ["UDP", "TCP"]
          content {
            port     = "53"
            protocol = ports.value
          }
        }
      }
    }
    dynamic "egress" {
      for_each = local.puffle_web_enabled ? [1] : []
      content {
        # NetworkPolicy has no FQDN rules. The application uses Spotify's HTTPS endpoints.
        to {
          ip_block {
            cidr   = "0.0.0.0/0"
            except = ["10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"]
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
