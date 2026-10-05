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
| [app/storage.py](app/storage.py) | Storage interface + Blob Storage and local-folder backends |
| [app/sqlserver_store.py](app/sqlserver_store.py) | Local SQL Server backend, auto-creates the database and tables |
| [static/](static/) | Customer chat page (`/`) and staff reference library (`/admin`) |
| [tests/](tests/) | Wear rules, image safety, and a check that the models match the OpenAPI contract |

## Local testing switches (no Azure, no cost)

Three settings in `.env` let you run the whole app on your PC without any Azure or paid service:

| Setting | Values | What it does |
|---|---|---|
| `DATA_BACKEND` | `sqlserver` | All data in a local SQL Server database. **Database, tables and indexes are created automatically on startup.** |
| | `local_files` | All data in `./data` as `.jpg` and `.json` files. Zero setup. |
| | `azure_blob` | Azure Blob Storage (production). |
| `AI_BACKEND` | `mock` | Fake AI that works offline and is free. It does **not** really look at the photo: the tread depth comes from a hash of the image, so different photos give different but repeatable results. Use it to test screens, the database and the km rules. |
| | `foundry` | Real Claude analysis on Microsoft Foundry (billed). |
| `DEV_AUTH_BYPASS` | `true` | Signs you in as a local admin without Entra ID. |

`sqlserver`, `local_files`, `mock` and `DEV_AUTH_BYPASS` are **refused at startup unless `APP_ENV=development`**, so they can't be switched on in production by accident. The startup log prints the active switches.

### What's stored in SQL Server

| Table | Contents |
|---|---|
| `reference_tyres` | Staff reference photos (`VARBINARY`) plus labels: condition, measured tread depth, km, size, notes |
| `customer_uploads` | Customer photos, keyed by the customer's user ID |
| `analyses` | The AI assessment and km estimate as JSON, plus `verdict`, `min_tread_mm`, `km_to_legal_limit` and `confidence` columns for easy reporting |
| `chat_messages` | Every question and answer, linked to its analysis |

Sign-in sessions stay in memory, so restarting the app just signs you in again.

**Mock reference data:** [sql/seed_reference_tyres.sql](sql/seed_reference_tyres.sql) inserts 17 mock reference tyres covering new to at-limit plus damaged, with placeholder images labelled "MOCK". Start the app once so the tables exist, then run the script. It's safe to re-run, and section 3 of the script removes the rows again. To change the rows, edit and re-run [sql/make_mock_seed.ps1](sql/make_mock_seed.ps1).

Example report:

```sql
SELECT verdict, COUNT(*) AS checks, AVG(km_to_legal_limit) AS avg_km_left
FROM dbo.analyses
WHERE created_at >= DATEADD(day, -30, SYSUTCDATETIME())
GROUP BY verdict;
```

### SQL Server setup (one time)

1. Install **SQL Server 2022 Express** or **Developer** edition. Both are free. Keep the default instance name `SQLEXPRESS`, or change `SQLSERVER_SERVER` to match.
2. Make sure the SQL Server service is running.
3. That's it. The app connects with **Windows authentication** (your Windows login, no password) and creates the `TyreAdvisor` database on first start. Your Windows account needs permission to create databases, which the installing user has by default.

The connection is encrypted. `SQLSERVER_TRUST_SERVER_CERTIFICATE=true` accepts the self-signed certificate of a local install; never use it against a shared or remote server.

## Run locally

Requires Python 3.10+. With `AI_BACKEND=mock` you don't need Azure at all. For real analysis (`AI_BACKEND=foundry`) you also need the Azure CLI and `az login`. There are still no keys: the app uses your login.

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
copy .env.example .env
```

The template is already set up for fully offline testing (`DATA_BACKEND=sqlserver`, `AI_BACKEND=mock`, `DEV_AUTH_BYPASS=true`). When you want real AI answers, set `AI_BACKEND=foundry` and `FOUNDRY_RESOURCE`, then run `az login`. Your account needs the **Foundry User** (formerly Azure AI User) role on that resource.

```bash
uvicorn app.main:app --reload --env-file .env
```
```bash
pytest
```

The SQL Server test is skipped by default. Run it against a throwaway `TyreAdvisorTest` database by setting the environment variable `TEST_SQLSERVER=1`.

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

- **Multiple instances:** sessions and the rate limiter are in memory. With the `azure_blob` backend, analyses and chat history are in memory too. For production, move analyses to **Azure Cosmos DB** and sessions to **Azure Cache for Redis**, both with Entra ID auth and no keys.
- **Accuracy:** photo estimates of tread depth are typically within about ±1 mm. That is fine for "about 20,000 km left", not for a legal certificate. Ask customers to put a coin or gauge in the groove. Build an evaluation set: 50+ photos with gauge-measured depths, then compare the bot's estimates and tune the prompt or reference set.
- **Legal minimum:** set `LEGAL_MIN_TREAD_MM` for your country (1.6 mm is common for cars).
- **Disclaimer:** keep the "AI estimate, not a professional inspection" message.
