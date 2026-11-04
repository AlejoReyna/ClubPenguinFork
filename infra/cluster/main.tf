terraform {
  required_version = ">= 1.11, < 2.0"
  backend "azurerm" {}
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}

variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/clubpenguin-pi.yaml"
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = "default"
}

provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = "default"
  }
}

resource "kubernetes_namespace_v1" "lab" {
  for_each = toset(["clubpenguin", "monitoring-lab"])
  metadata {
    name = each.value
    labels = {
      "app.kubernetes.io/part-of" = "clubpenguin-lab"
    }
  }
  lifecycle { prevent_destroy = true }
}

resource "random_password" "grafana" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "grafana" {
  metadata {
    name      = "grafana-admin"
    namespace = kubernetes_namespace_v1.lab["monitoring-lab"].metadata[0].name
  }
  data = {
    admin-user     = "admin"
    admin-password = random_password.grafana.result
  }
}

resource "helm_release" "prometheus" {
  name       = "prometheus-lab"
  namespace  = kubernetes_namespace_v1.lab["monitoring-lab"].metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = "29.40.3"
  timeout    = 600
  atomic     = true
  values = concat([file("${path.module}/../../monitoring/prometheus.yaml")],
    var.bots_enabled ? [file("${path.module}/../../monitoring/bots-rules.yaml")] : [],
  var.web_enabled ? [file("${path.module}/../../monitoring/puffle-rules.yaml")] : [])
}

resource "helm_release" "grafana" {
  name       = "grafana-lab"
  namespace  = kubernetes_namespace_v1.lab["monitoring-lab"].metadata[0].name
  repository = "https://grafana-community.github.io/helm-charts"
  chart      = "grafana"
  version    = "14.3.1"
  timeout    = 600
  atomic     = true
  values = [
    file("${path.module}/../../monitoring/grafana.yaml"),
    yamlencode({
      dashboards = {
        default = {
          raspberry = {
            json = file("${path.module}/../../monitoring/dashboard.json")
          }
        }
      }
    })
  ]
  depends_on = [kubernetes_secret_v1.grafana, helm_release.prometheus]
}

output "grafana_access" {
  value = "kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n monitoring-lab port-forward svc/grafana-lab 3000:80"
}

