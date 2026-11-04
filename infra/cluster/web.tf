variable "web_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.web_enabled || var.game_enabled
    error_message = "Enable the game before deploying the web client."
  }
}

variable "web_image_tag" {
  type    = string
  default = null
}

# Public hostname allowed to open the game WebSocket (see socket_proxy.py).
variable "public_hostname" {
  type    = string
  default = ""
}

resource "kubernetes_deployment_v1" "web" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "web"
    namespace = local.game_namespace
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "web" } }
    template {
      metadata { labels = merge(local.game_labels, { app = "web" }) }
      spec {
        automount_service_account_token = false
        image_pull_secrets { name = kubernetes_secret_v1.registry[0].metadata[0].name }
        container {
          name  = "web"
          image = "${local.registry}/clubpenguin/web:${coalesce(var.web_image_tag, var.image_tag)}"
          port { container_port = 8080 }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "300m", memory = "128Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
          }
        }
        container {
          name  = "socket-proxy"
          image = local.runtime_image
          env {
            name  = "MODE"
            value = "proxy"
          }
          env {
            name  = "ALLOWED_ORIGINS"
            value = join(",", distinct(compact(concat([var.public_hostname], var.proxy_extra_hosts))))
          }
          port { container_port = 8765 }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "200m", memory = "128Mi" }
          }
          security_context {
            run_as_non_root            = true
            run_as_user                = 10001
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
          readiness_probe {
            tcp_socket { port = 8765 }
          }
        }
      }
    }
  }
  # The first pull of a new web image (media layers) took ~8.5 minutes on the Pi's link;
  # the provider's 10-minute default would fail an otherwise healthy rollout.
  timeouts {
    create = "25m"
    update = "25m"
  }
  depends_on = [kubernetes_deployment_v1.runtime]
}

resource "kubernetes_service_v1" "web" {
  count = var.web_enabled ? 1 : 0
  metadata {
    name      = "web"
    namespace = local.game_namespace
  }
  spec {
    type     = "NodePort"
    selector = { app = "web" }
    port {
      port        = 8080
      target_port = 8080
      node_port   = 30080
    }
  }
}

variable "proxy_extra_hosts" {
  description = "Additional hostnames or IPs allowed by the WebSocket proxy."
  type        = list(string)
  default     = []
}
