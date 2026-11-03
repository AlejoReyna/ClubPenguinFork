# Cloudflare Tunnel connector for club.example.com.
# The tunnel token is NOT managed here: create the Secret by hand so it never
# reaches the Terraform state or Git (see docs/acceso-publico.md).
variable "cloudflared_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.cloudflared_enabled || var.web_enabled
    error_message = "Enable the web client before deploying the tunnel connector."
  }
}

variable "cloudflared_image" {
  type    = string
  default = "cloudflare/cloudflared:2026.10.7"
}

resource "kubernetes_deployment_v1" "cloudflared" {
  count = var.cloudflared_enabled ? 1 : 0
  metadata {
    name      = "cloudflared"
    namespace = local.game_namespace
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "cloudflared" } }
    template {
      metadata { labels = { app = "cloudflared" } }
      spec {
        automount_service_account_token = false
        container {
          name  = "cloudflared"
          image = var.cloudflared_image
          args  = ["tunnel", "--no-autoupdate", "run", "--token", "$(TUNNEL_TOKEN)"]
          env {
            name = "TUNNEL_TOKEN"
            value_from {
              secret_key_ref {
                name = "cloudflared-token"
                key  = "token"
              }
            }
          }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "200m", memory = "128Mi" }
          }
          security_context {
            run_as_non_root            = true
            run_as_user                = 65532
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
        }
      }
    }
  }
}

# game-ingress only admits pods of the lab; let the connector reach the web
# service on 8080 and nothing else.
resource "kubernetes_network_policy_v1" "web_from_cloudflared" {
  count = var.cloudflared_enabled ? 1 : 0
  metadata {
    name      = "web-from-cloudflared"
    namespace = local.game_namespace
  }
  spec {
    pod_selector { match_labels = { app = "web" } }
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector { match_labels = { app = "cloudflared" } }
      }
      ports {
        port     = "8080"
        protocol = "TCP"
      }
    }
  }
}
