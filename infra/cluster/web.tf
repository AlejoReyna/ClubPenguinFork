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

# Navbar "Infraestructura" panel (containers/runtime/panel_summary.py, MODE=panel). A pod-local
# sidecar: nginx proxies /infra/summary.json to 127.0.0.1:8766, so the browser only ever sees an
# aggregated same-origin summary, never the Prometheus/Grafana addresses configured here.
variable "panel_prometheus_url" {
  type        = string
  default     = "http://prometheus.monitoring-lab.svc.cluster.local"
  description = "Cluster-internal Prometheus base URL for the panel. Empty shows \"Sin configurar\"."
}

variable "panel_grafana_url" {
  type        = string
  default     = ""
  description = "Authorised https dashboard URL. Empty keeps Grafana private (port-forward), shown as \"Acceso privado\"."
}

variable "panel_show_players" {
  type        = bool
  default     = false
  description = "Show the connected-player count in the panel. Only enable once that figure is approved for this audience."
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
          name  = "panel-summary"
          image = local.runtime_image
          env {
            name  = "MODE"
            value = "panel"
          }
          env {
            name  = "PANEL_PROJECT"
            value = "ClubPenguin Lab"
          }
          env {
            name  = "PANEL_PROVIDER"
            value = "Raspberry Pi 5 (autoalojado)"
          }
          env {
            name  = "PANEL_PLATFORM"
            value = "Kubernetes (k3s)"
          }
          env {
            name  = "PANEL_ENVIRONMENT"
            value = "lab"
          }
          env {
            name  = "PANEL_COMPONENTS"
            value = "Web (nginx), Runtime Houdini, Puffle, PostgreSQL, Redis, Prometheus, Grafana"
          }
          env {
            name  = "PROMETHEUS_URL"
            value = var.panel_prometheus_url
          }
          env {
            name  = "GRAFANA_URL"
            value = var.panel_grafana_url
          }
          env {
            name  = "GRAFANA_PRIVATE"
            value = var.panel_grafana_url == "" ? "true" : "false"
          }
          env {
            name  = "PANEL_SHOW_PLAYERS"
            value = tostring(var.panel_show_players)
          }
          volume_mount {
            name       = "release-info"
            mount_path = "/etc/release-info"
            read_only  = true
          }
          resources {
            requests = { cpu = "10m", memory = "24Mi" }
            limits   = { cpu = "100m", memory = "64Mi" }
          }
          security_context {
            run_as_non_root            = true
            run_as_user                = 10001
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
        }
        # Optional: the ConfigMap only exists when var.release_info is set (release.tf). Only the
        # `commit` and `version` keys are ever read by the sidecar.
        volume {
          name = "release-info"
          config_map {
            name     = "release-info"
            optional = true
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
