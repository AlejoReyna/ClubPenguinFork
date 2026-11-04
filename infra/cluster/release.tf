# Records which release is live, so a rollback can identify the previous one
# from the cluster itself. Only non-secret identifiers belong here: commit,
# image tags and the pipeline run. Unset (the default) creates nothing, so a
# plain `terraform plan` from a clean checkout is unaffected.
variable "release_info" {
  type        = map(string)
  default     = {}
  description = "Non-secret release identifiers published as the release-info ConfigMap."
}

resource "kubernetes_config_map_v1" "release" {
  count = var.game_enabled && length(var.release_info) > 0 ? 1 : 0
  metadata {
    name      = "release-info"
    namespace = local.game_namespace
    labels    = local.game_labels
  }
  data = var.release_info
}
