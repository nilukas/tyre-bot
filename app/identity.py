"""Azure credentials. No keys, passwords or client secrets anywhere.

* Production: the app's user-assigned Managed Identity.
* Development: your own `az login` session (AzureCliCredential).
"""

from __future__ import annotations

from azure.identity import AzureCliCredential, ManagedIdentityCredential

from .config import Settings

# Audience Entra ID expects for a federated (workload identity) client assertion.
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
# Scope for Claude on Microsoft Foundry.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def build_azure_credential(settings: Settings):
    if settings.is_production:
        return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return AzureCliCredential()
