# Tyre Advisor

A web chat bot that looks at a photo of a tyre and tells the customer its condition and roughly how many kilometres it has left.

- **Staff** (Entra ID app role `Tyre.Admin`) upload photos of new and used tyres. Each photo is labelled with the tread depth measured by a gauge. These become the bot's **reference library**.
- **Customers** sign in, upload a photo of their tyre and ask questions such as "how many more km can I drive?"

## How it works

```
Browser ──cookie──▶ FastAPI BFF (Azure Container Apps / App Service, user-assigned Managed Identity)
                      │
                      ├─ Entra ID sign-in (MSAL confidential client, Managed Identity federated credential, no secret)
                      ├─ Pillow: validate + re-encode photo (strips EXIF/GPS)
                      ├─ Blob Storage (Managed Identity): reference photos + customer uploads
                      ├─ Claude on Microsoft Foundry (Managed Identity):
                      │     reference photos + customer photo ─▶ structured TyreVisionAssessment
                      │     (tread depth per region, wear pattern, defects, DOT age, confidence)
                      └─ wear.py: deterministic km calculation + safety rules
```

The AI only reports **what it sees**. The kilometre numbers are calculated in Python ([app/wear.py](app/wear.py)), so they are consistent and testable:

- km left = (shallowest tread − legal minimum) × km-per-mm.
- km-per-mm is measured from the customer's own data when they give "km since fitted". Otherwise it is a default for the vehicle type.
- Irregular wear (alignment, pressure, suspension) lowers the rate and adds advice.
- **Safety overrides always win.** A bulge, exposed cords, tread at or below the legal limit, or the wear indicator flush with the tread gives 0 km and "replace now". Tyres over 6 years old get an inspection warning; over 10 years, a replacement warning.
- The answer is a range (low to high) based on how certain the photo estimate is.

## Project layout

| Path | Purpose |
|---|---|
| [openapi.yaml](openapi.yaml) | **API contract (source of truth).** Generate clients from it, e.g. with Microsoft Kiota. |
| [app/main.py](app/main.py) | Routes, security headers, wiring |
| [app/auth.py](app/auth.py) | BFF login, server-side sessions, CSRF, roles, rate limit |
| [app/analysis.py](app/analysis.py) | Claude vision prompt, reference selection, follow-up chat |
| [app/wear.py](app/wear.py) | Remaining-km calculator and safety rules |
| [app/images.py](app/images.py) | Upload validation (OWASP file-upload guidance) |
| [app/storage.py](app/storage.py) | Blob Storage, or a local folder in development |
| [static/](static/) | Customer chat page (`/`) and staff reference library (`/admin`) |
| [tests/](tests/) | Wear rules, image safety, and a check that the models match the OpenAPI contract |

## Run locally

Requires Python 3.10+ and the Azure CLI. There are no keys: locally the app uses your `az login`.

```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate
```
```bash
pip install -r requirements-dev.txt
```
```bash
az login
```
```bash
copy .env.example .env
```

Edit `.env` and set `FOUNDRY_RESOURCE`. Your own account needs the **Foundry User** (formerly Azure AI User) role on that resource. `DEV_AUTH_BYPASS=true` signs you in as a local admin, and only works with `APP_ENV=development`.

```bash
uvicorn app.main:app --reload --env-file .env
```
```bash
pytest
```

Open http://localhost:8000. First go to **/admin** and add 10–20 labelled photos covering new, half-worn, at-the-limit and damaged tyres. Then try the customer page.

## Azure setup (production)

1. **Foundry:** create a Foundry resource and deploy `claude-opus-5` (Hosted on Azure). **Disable local (key) auth** on the resource.
2. **Identity:** create a user-assigned Managed Identity, e.g. `id-tyre-advisor`, and attach it to the Container App or App Service.
3. **RBAC for the identity:**
   - **Foundry User** (formerly Azure AI User) on the Foundry resource
   - **Storage Blob Data Contributor** on the storage account (or scoped to the two containers)
4. **Storage:** set `allowSharedKeyAccess=false` and turn off public blob access. Create the `tyre-references` and `tyre-uploads` containers. Add a **lifecycle rule** that deletes `tyre-uploads` after your retention period, e.g. 30 days (privacy / data minimisation).
5. **App registration (Entra ID):**
   - Web redirect URI: `https://<your-host>/auth/callback`
   - **Do not create a client secret.** Under *Certificates & secrets → Federated credentials*, add the *Managed identity* scenario and select `id-tyre-advisor` (audience `api://AzureADTokenExchange`).
   - Create the app role `Tyre.Admin` and assign it to your staff group.
6. **App settings:** `APP_ENV=production`, `PUBLIC_BASE_URL`, `FOUNDRY_RESOURCE`, `CLAUDE_DEPLOYMENT`, `ENTRA_AUTHORITY`, `ENTRA_CLIENT_ID`, `MANAGED_IDENTITY_CLIENT_ID`, `STORAGE_ACCOUNT_URL`. HTTPS only.

> **Customers in Entra External ID:** if customers sign in through a separate External ID (CIAM) tenant, set `ENTRA_AUTHORITY` to `https://<tenant>.ciamlogin.com/`. Check that the app registration in that tenant can trust your Managed Identity as a federated credential across tenants. If it can't, use a certificate held in Key Vault (non-exportable key) as the client credential instead of a secret.

## Security notes

- **No secrets anywhere.** Foundry, Storage and the Entra client credential all use the Managed Identity. Turn off key-based auth on Foundry and Storage so keys can't be used even by accident.
- **BFF pattern:** OAuth tokens never reach the browser. The session cookie is `__Host-`, `Secure`, `HttpOnly`, `SameSite=Lax`, and holds a random ID only. The session ID is rotated at login. MSAL handles PKCE, state and nonce.
- **CSRF:** every POST/DELETE needs the per-session `X-CSRF-Token` header, and the `Origin` must match.
- **Authorization:** every `/api/*` route requires a session. The reference library also requires the `Tyre.Admin` role on the server side. Customers can only reach their own analyses (session-scoped), and their uploads are stored under their Entra object ID.
- **Uploads:** size-capped, format detected from the bytes, decompression-bomb protection, and re-encoded to JPEG. EXIF/GPS and hidden payloads are stripped, and filenames are server-generated.
- **Prompt injection:** text inside photos and chat messages is treated as data. The model's output is schema-constrained, and safety verdicts come from code, not from the model.
- **Headers:** strict CSP (no inline script), HSTS, nosniff, no-referrer, frame-ancestors none. API docs are disabled in production.
- **Rate limiting:** 30 photo checks per user per hour in the app. Also put **Azure Front Door + WAF** in front for edge rate limiting and bot protection.

## Before going live

- **Multiple instances:** sessions and the rate limiter are in memory. With more than one instance, move them to **Azure Cache for Redis** with Entra ID auth.
- **Accuracy:** photo estimates of tread depth are typically within about ±1 mm. That is fine for "about 20,000 km left", not for a legal certificate. Ask customers to put a coin or gauge in the groove. Build an evaluation set: 50+ photos with gauge-measured depths, then compare the bot's estimates and tune the prompt or reference set.
- **Legal minimum:** set `LEGAL_MIN_TREAD_MM` for your country (1.6 mm is common for cars).
- **Disclaimer:** keep the "AI estimate, not a professional inspection" message.
