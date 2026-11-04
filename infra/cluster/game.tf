variable "game_enabled" {
  type    = bool
  default = false
}
variable "image_tag" {
  type        = string
  default     = "lab-001"
  description = "Fallback tag for runtime and database when their own tags are not set."
}

# Per-component tags let a release change one image without touching the others
# (for example, a frontend-only release must not restart PostgreSQL). Both fall
# back to image_tag, so existing tfvars keep working unchanged.
variable "runtime_image_tag" {
  type        = string
  default     = null
  description = "Tag of clubpenguin/runtime (login, world, dash, exporter and the socket proxy)."
}

variable "database_image_tag" {
  type        = string
  default     = null
  description = "Tag of clubpenguin/database (PostgreSQL)."
}

# Where the `cloud` root stores its state. No default: deploy.py passes it from
# deploy.env, and manual runs read it from the private terraform.tfvars.
variable "cloud_state" {
  type = object({
    resource_group_name  = string
    storage_account_name = string
    container_name       = string
    key                  = string
  })
}

# LAN allowed to reach the game ports. The default is the anonymized sample from the public
# repository; a real deployment MUST override it (the pipeline requires CP_LAN_CIDR), or the
# live policy would be rewritten to the sample range.
variable "lan_cidr" {
  type    = string
  default = "192.168.0.0/24"
}

data "terraform_remote_state" "cloud" {
  backend = "azurerm"
  config = {
    resource_group_name  = var.cloud_state.resource_group_name
    storage_account_name = var.cloud_state.storage_account_name
    container_name       = var.cloud_state.container_name
    key                  = var.cloud_state.key
    use_azuread_auth     = true
  }
}

locals {
  game_namespace = kubernetes_namespace_v1.lab["clubpenguin"].metadata[0].name
  game_labels    = { "app.kubernetes.io/part-of" = "clubpenguin-lab" }
  registry       = data.terraform_remote_state.cloud.outputs.registry_login_server
  runtime_image  = "${local.registry}/clubpenguin/runtime:${coalesce(var.runtime_image_tag, var.image_tag)}"
  database_image = "${local.registry}/clubpenguin/database:${coalesce(var.database_image_tag, var.image_tag)}"
  runtimes = var.game_enabled ? {
    login    = { mode = "login", port = 6112, cpu = "250m", memory = "384Mi", node_port = 30612 }
    glaciar  = { mode = "world", port = 9876, cpu = "750m", memory = "768Mi", node_port = 30976 }
    dash     = { mode = "dash", port = 3000, cpu = "500m", memory = "512Mi", node_port = 0 }
    exporter = { mode = "exporter", port = 9101, cpu = "100m", memory = "128Mi", node_port = 0 }
  } : {}
}

resource "random_password" "postgres" {
  count   = var.game_enabled ? 1 : 0
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "postgres" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "postgres-credentials"
    namespace = local.game_namespace
  }
  data = { POSTGRES_PASSWORD = random_password.postgres[0].result }
}

resource "kubernetes_secret_v1" "registry" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "acr-pull"
    namespace = local.game_namespace
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        (local.registry) = {
          username = data.terraform_remote_state.cloud.outputs.registry_pull_username
          password = data.terraform_remote_state.cloud.outputs.registry_pull_password
          auth     = base64encode("${data.terraform_remote_state.cloud.outputs.registry_pull_username}:${data.terraform_remote_state.cloud.outputs.registry_pull_password}")
        }
      }
    })
  }
}

resource "kubernetes_persistent_volume_claim_v1" "postgres" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "postgres-data"
    namespace = local.game_namespace
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources { requests = { storage = "4Gi" } }
  }
  wait_until_bound = false
  lifecycle { prevent_destroy = true }
}

resource "kubernetes_stateful_set_v1" "postgres" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "postgres"
    namespace = local.game_namespace
  }
  spec {
    service_name = "postgres"
    replicas     = 1
    selector { match_labels = { app = "postgres" } }
    template {
      metadata { labels = merge(local.game_labels, { app = "postgres" }) }
      spec {
        automount_service_account_token = false
        image_pull_secrets { name = kubernetes_secret_v1.registry[0].metadata[0].name }
        container {
          name  = "postgres"
          image = local.database_image
          args  = ["-c", "shared_buffers=64MB", "-c", "max_connections=40"]
          port { container_port = 5432 }
          env {
            name  = "POSTGRES_DB"
            value = "houdini"
          }
          env {
            name  = "POSTGRES_USER"
            value = "houdini"
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }
          env_from {
            secret_ref { name = kubernetes_secret_v1.postgres[0].metadata[0].name }
          }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          readiness_probe {
            exec { command = ["pg_isready", "-U", "houdini", "-d", "houdini"] }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          startup_probe {
            exec { command = ["pg_isready", "-U", "houdini", "-d", "houdini"] }
            failure_threshold = 30
            period_seconds    = 5
          }
        }
        volume {
          name = "data"
          persistent_volume_claim { claim_name = kubernetes_persistent_volume_claim_v1.postgres[0].metadata[0].name }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "postgres" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "postgres"
    namespace = local.game_namespace
  }
  spec {
    selector = { app = "postgres" }
    port {
      port        = 5432
      target_port = 5432
    }
  }
}

resource "kubernetes_deployment_v1" "redis" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "redis"
    namespace = local.game_namespace
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "redis" } }
    template {
      metadata { labels = merge(local.game_labels, { app = "redis" }) }
      spec {
        automount_service_account_token = false
        container {
          name  = "redis"
          image = "redis:7.4-alpine"
          args  = ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]
          port { container_port = 6379 }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "200m", memory = "128Mi" }
          }
          readiness_probe {
            exec { command = ["redis-cli", "ping"] }
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "redis" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "redis"
    namespace = local.game_namespace
  }
  spec {
    selector = { app = "redis" }
    port {
      port        = 6379
      target_port = 6379
    }
  }
}

resource "kubernetes_deployment_v1" "runtime" {
  for_each = local.runtimes
  metadata {
    name      = each.key
    namespace = local.game_namespace
  }
  spec {
    replicas = 1
    strategy { type = "Recreate" }
    selector { match_labels = { app = each.key } }
    template {
      metadata {
        labels = merge(local.game_labels, { app = each.key })
        annotations = each.key == "exporter" ? {
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "9101"
        } : {}
      }
      spec {
        automount_service_account_token = false
        image_pull_secrets { name = kubernetes_secret_v1.registry[0].metadata[0].name }
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          fs_group        = 10001
        }
        container {
          name  = each.key
          image = local.runtime_image
          port {
            name           = "app"
            container_port = each.value.port
          }
          dynamic "env" {
            for_each = merge({
              MODE           = each.value.mode
              PORT           = tostring(each.value.port)
              POSTGRES_HOST  = "postgres"
              POSTGRES_USER  = "houdini"
              POSTGRES_DB    = "houdini"
              REDIS_HOST     = "redis"
              WORLD_CAPACITY = "20"
              }, each.key == "glaciar" ? {
              PUFFLE_ENABLED         = tostring(var.puffle_enabled)
              PUFFLE_SECRET_FILE     = "/etc/puffle/secret"
              PUFFLE_ALLOWED_PLAYERS = join(",", var.puffle_allowed_players)
            } : {})
            content {
              name  = env.key
              value = env.value
            }
          }
          dynamic "env_from" {
            for_each = each.key == "exporter" ? [] : [1]
            content {
              secret_ref { name = kubernetes_secret_v1.postgres[0].metadata[0].name }
            }
          }
          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = each.value.cpu, memory = each.value.memory }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
          readiness_probe {
            tcp_socket { port = each.value.port }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          startup_probe {
            tcp_socket { port = each.value.port }
            failure_threshold = 30
            period_seconds    = 5
          }
          dynamic "volume_mount" {
            for_each = each.key == "glaciar" && var.puffle_enabled ? [1] : []
            content {
              name       = "puffle-secret"
              mount_path = "/etc/puffle"
              read_only  = true
            }
          }
        }
        dynamic "volume" {
          for_each = each.key == "glaciar" && var.puffle_enabled ? [1] : []
          content {
            name = "puffle-secret"
            secret {
              secret_name  = var.puffle_service_secret
              default_mode = "0440"
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_stateful_set_v1.postgres, kubernetes_deployment_v1.redis]
}

resource "kubernetes_service_v1" "runtime" {
  for_each = local.runtimes
  metadata {
    name      = each.key
    namespace = local.game_namespace
  }
  spec {
    selector = { app = each.key }
    type     = each.value.node_port == 0 ? "ClusterIP" : "NodePort"
    port {
      port        = each.value.port
      target_port = each.value.port
      node_port   = each.value.node_port == 0 ? null : each.value.node_port
    }
  }
}

resource "kubernetes_network_policy_v1" "game" {
  count = var.game_enabled ? 1 : 0
  metadata {
    name      = "game-ingress"
    namespace = local.game_namespace
  }
  spec {
    pod_selector {
      match_expressions {
        key      = "app"
        operator = "NotIn"
        values   = ["puffle"]
      }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector { match_labels = local.game_labels }
      }
    }
    ingress {
      from {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "monitoring-lab" } }
      }
      ports {
        port     = "9101"
        protocol = "TCP"
      }
    }
    ingress {
      from {
        ip_block { cidr = var.lan_cidr }
      }
      dynamic "ports" {
        for_each = [6112, 9876, 8080]
        content {
          port     = tostring(ports.value)
          protocol = "TCP"
        }
      }
    }
  }
}
