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

# Navbar «Diálogos» panel (containers/runtime/dialog_feed.py, MODE=dialog; docs/dialogos-publicos.md).
# A pod-local sidecar mounts the bots dialog-log PVC and its switch ConfigMap READ-ONLY and serves an
# allow-listed feed (no player hash, no human message); nginx proxies /dialog/feed.json to it. Off by
# default: without the sidecar nginx answers 404 and the page hides the button. The switch ConfigMap
# (bots-dialog-log) also empties the public feed when it is off.
# The PVC is ReadWriteOnce on local-path: it works because web and bots share the Pi's only node.
# A multi-node cluster would need RWX storage or a different reader.
variable "dialog_feed_enabled" {
  type        = bool
  default     = false
  description = "Publish the filtered bots dialog feed behind the navbar «Diálogos» button."
  validation {
    condition     = !var.dialog_feed_enabled || (var.web_enabled && var.bots_enabled)
    error_message = "The dialog feed reads the bots dialog log: enable web and bots first."
  }
}

locals {
  dialog_feed_pvc       = var.dialog_feed_enabled ? one(kubernetes_persistent_volume_claim_v1.bots_dialog[*].metadata[0].name) : null
  dialog_feed_configmap = var.dialog_feed_enabled ? one(kubernetes_config_map_v1.bots_dialog_log[*].metadata[0].name) : null
}

# egress-internal-only keeps the web pod inside its own namespace (plus DNS). The panel sidecar
# needs exactly one more destination: the Prometheus server in monitoring-lab, port 9090. Policies
# are additive, so this opens nothing else (no Grafana, no node-exporter, no other namespace).
resource "kubernetes_network_policy_v1" "web_to_prometheus" {
  count = var.web_enabled && var.panel_prometheus_url != "" ? 1 : 0
  metadata {
    name      = "web-to-prometheus"
    namespace = local.game_namespace
  }
  spec {
    pod_selector { match_labels = { app = "web" } }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "monitoring-lab" } }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "prometheus"
            "app.kubernetes.io/component" = "server"
          }
        }
      }
      ports {
        port     = "9090"
        protocol = "TCP"
      }
    }
  }
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
        dynamic "container" {
          for_each = var.dialog_feed_enabled ? [1] : []
          content {
            name  = "dialog-feed"
            image = local.runtime_image
            env {
              name  = "MODE"
              value = "dialog"
            }
            volume_mount {
              name       = "dialog-log"
              mount_path = "/var/lib/bots-dialog"
              read_only  = true
            }
            volume_mount {
              name       = "dialog-switch"
              mount_path = "/etc/bots-dialog"
              read_only  = true
            }
            resources {
              requests = { cpu = "10m", memory = "24Mi" }
              limits   = { cpu = "100m", memory = "64Mi" }
            }
            security_context {
              run_as_non_root            = true
              run_as_user                = 10001 # owner of the 0600 day files written by the bots pod
              run_as_group               = 10001
              allow_privilege_escalation = false
              read_only_root_filesystem  = true
              capabilities { drop = ["ALL"] }
            }
          }
        }
        # Read-only at the volume too, and no pod fs_group: the kubelet never re-owns or widens the
        # bots' private log files for this reader.
        dynamic "volume" {
          for_each = var.dialog_feed_enabled ? [1] : []
          content {
            name = "dialog-log"
            persistent_volume_claim {
              claim_name = local.dialog_feed_pvc
              read_only  = true
            }
          }
        }
        dynamic "volume" {
          for_each = var.dialog_feed_enabled ? [1] : []
          content {
            name = "dialog-switch"
            config_map { name = local.dialog_feed_configmap }
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
