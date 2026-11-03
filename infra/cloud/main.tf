variable "subscription_id" { type = string }
variable "registry_name" {
  type    = string
  default = "acrdevopslabn3fc0"
}
variable "registry_resource_group" {
  type    = string
  default = "rg-devopslab-lab"
}
variable "storage_account_name" {
  type    = string
  default = "sttfstate2188627882"
}
variable "storage_resource_group" {
  type    = string
  default = "rg-tfstate"
}

# These resources already have owners in other Terraform states.
data "azurerm_container_registry" "existing" {
  name                = var.registry_name
  resource_group_name = var.registry_resource_group
}
data "azurerm_storage_account" "existing" {
  name                = var.storage_account_name
  resource_group_name = var.storage_resource_group
}

resource "azurerm_storage_container" "backups" {
  name                  = "clubpenguin-backups"
  storage_account_id    = data.azurerm_storage_account.existing.id
  container_access_type = "private"
  lifecycle { prevent_destroy = true }
}

output "registry_login_server" { value = data.azurerm_container_registry.existing.login_server }
output "backup_container" { value = azurerm_storage_container.backups.name }
output "storage_account_name" { value = data.azurerm_storage_account.existing.name }

resource "azurerm_container_registry_scope_map" "pi_pull" {
  name                    = "clubpenguin-pi-pull"
  container_registry_name = data.azurerm_container_registry.existing.name
  resource_group_name     = var.registry_resource_group
  actions = flatten([
    for repository in ["runtime", "database", "web"] : [
      "repositories/clubpenguin/${repository}/content/read",
      "repositories/clubpenguin/${repository}/metadata/read"
    ]
  ])
}

resource "azurerm_container_registry_token" "pi_pull" {
  name                    = "clubpenguin-pi-pull"
  container_registry_name = data.azurerm_container_registry.existing.name
  resource_group_name     = var.registry_resource_group
  scope_map_id            = azurerm_container_registry_scope_map.pi_pull.id
}

resource "azurerm_container_registry_token_password" "pi_pull" {
  container_registry_token_id = azurerm_container_registry_token.pi_pull.id
  password1 {
    expiry = "2027-02-01T00:00:00Z"
  }
}

output "registry_pull_username" {
  value = azurerm_container_registry_token.pi_pull.name
}
output "registry_pull_password" {
  value     = azurerm_container_registry_token_password.pi_pull.password1[0].value
  sensitive = true
}

