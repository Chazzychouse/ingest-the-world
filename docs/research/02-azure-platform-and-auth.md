# 02 — Azure Platform, Storage, Pipeline, and Auth

Research date: 2026-10-24. Primary sources only (learn.microsoft.com, Azure GitHub, tus.io for the protocol spec, HashiCorp provider source). Each claim cites a source ID; the source list is at the bottom. **Date-sensitive** facts are marked (DS). **Unverified or inferred** items are marked (?).

---

## TL;DR

1. **Use direct-to-blob uploads.** The browser sends bytes straight to Blob Storage with a short-lived, single-blob **user delegation SAS**, using Put Block plus Put Block List. Block blobs allow 50,000 blocks of up to 4000 MiB each (about 190.7 TiB in total), and uncommitted blocks persist for 7 days, so resumable, parallel upload comes from the storage layer itself. A tus server is not needed, and the API never proxies gigabytes. [S5][S6]
2. **Pipeline:** `BlobCreated` (filtered to `PutBlockList`/`FlushWithClose`) → Event Grid → Service Bus queue → container workers. Events are at-least-once and can arrive out of order, so workers must be idempotent, keyed on blob URL + eTag/sequencer. [S9][S10]
3. **Compute for multi-GB files: containers, not Functions.** Flex Consumption caps at 4 GB memory and 0.8 GB temp storage per instance. Container Apps jobs (Consumption profile up to 4 vCPU / 8 GiB; Dedicated E-series up to 256 GiB) or AKS + KEDA fit better. [S11][S12][S13]
4. **Gov-cloud compliance gap:** in Azure Government, **Container Apps is in scope only for FedRAMP High + DoD IL2, not IL4/IL5**. AKS, Functions, Postgres, Event Grid, Service Bus, Blob, Key Vault, Cosmos, AI Search, and Monitor cover IL2–IL5 (most also IL6). If IL4/IL5 is ever required, plan on AKS. (DS, list "last updated Feb 2026") [S2]
5. **Postgres Flexible Server** runs in all three US Gov regions. Zone-redundant HA and geo-redundant backup are available **only in US Gov Virginia**. The extension catalog includes PostGIS 3.6.1, pgvector 0.8.2, TimescaleDB 2.24, and pg_partman 5.4.3 (for PG16–18). Whether the catalog is identical in Gov is unverified (?). [S14][S15]
6. **Local-dev gaps:** Azurite does **not** support ADLS Gen2 (HNS). The official Service Bus emulator exists but has no Entra auth and no persistence. Microsoft ships no official Event Grid emulator. [S20][S21][S22]

---

## 1. Azure Government: endpoints, availability, compliance

### 1.1 Endpoint differences (Commercial → Gov) [S1][S16]

| Service | Commercial | Azure Government |
|---|---|---|
| Entra ID authority | `login.microsoftonline.com` | `login.microsoftonline.us` |
| Portal | `portal.azure.com` | `portal.azure.us` |
| ARM | `management.azure.com` | `management.usgovcloudapi.net` |
| Blob / Queue / Table / File | `*.blob.core.windows.net` etc. | `*.blob.core.usgovcloudapi.net` etc. |
| ADLS Gen2 (dfs) | `*.dfs.core.windows.net` | `*.dfs.core.usgovcloudapi.net` (?) (pattern inferred; not in the table) |
| Key Vault | `vault.azure.net` | `vault.usgovcloudapi.net` |
| Service Bus / Event Hubs | `servicebus.windows.net` | `servicebus.usgovcloudapi.net` |
| PostgreSQL | `postgres.database.azure.com` | `postgres.database.usgovcloudapi.net` |
| Cosmos DB | `documents.azure.com` | `documents.azure.us` |
| AI Search | `search.windows.net` | `search.azure.us` |
| Azure Maps | `atlas.microsoft.com` | `atlas.azure.us` |
| Container Registry | `azurecr.io` | `azurecr.us` |
| App Service / Functions | `azurewebsites.net` | `azurewebsites.us` |
| App Insights ingestion | `{region}.in.applicationinsights.azure.com` | `{region}.in.applicationinsights.azure.us` |
| Log Analytics API | `api.loganalytics.io` | `api.loganalytics.us` |
| Microsoft Graph | `graph.microsoft.com` | `graph.microsoft.us` (example in MSAL doc) [S17] |

- **Don't hard-code suffixes.** Run `az cloud show --name AzureUSGovernment` to get every endpoint for a cloud [S1]. App config should carry a single `cloud` setting (or explicit endpoint URLs) that drives the authority, storage suffix, and ARM host.
- **App Insights in Gov:** the SDK endpoints must be overridden. Use a connection string; for Functions, set `APPLICATIONINSIGHTS_CONNECTION_STRING` [S1].
- **Naming:** don't put export-controlled or sensitive information in Azure resource names [S1].
- **Gov limitations relevant here** [S1]:
  - Azure AD B2C is not available.
  - App Service Managed Certificates are not available.
  - MFA "Trusted IPs" is not supported; use Conditional Access named locations instead.
  - Cosmos DB for PostgreSQL (Citus) is not available. Flexible Server is.

### 1.2 Service availability and compliance scope (Azure Government regions) (DS, "Last updated: February 2026") [S2]

| Service | FedRAMP High | IL2 | IL4 | IL5 (WI) | IL6 (Gov Secret) | Notes |
|---|:-:|:-:|:-:|:-:|:-:|---|
| Storage: Blobs (incl. ADLS Gen2) | ✅ | ✅ | ✅ | ✅ | ✅ | Archive tier: no IL6 |
| Event Grid | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Service Bus | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Functions | ✅ | ✅ | ✅ | ✅ | ✅ | Flex Consumption availability in Gov is **unverified** (?) — check `az functionapp list-flexconsumption-locations` against a Gov subscription [S12] |
| **Container Apps** | ✅ | ✅ | ❌ | ❌ | ❌ | **Main constraint.** Region list in Gov not confirmed from a primary source (?). A 2024 Azure Updates item reported a preview in US Gov Virginia, but that page now returns no content. |
| AKS | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Azure Database for PostgreSQL | ✅ | ✅ | ✅ | ✅ | ✅ | Flexible Server in US Gov Arizona, Texas, and Virginia; ZR-HA and geo-backup only in Virginia [S14] |
| Cosmos DB | ✅ | ✅ | ✅ | ✅ | ✅ | |
| AI Search | ✅ | ✅ | ✅ | ✅ | ✅ | Semantic ranker and agentic retrieval in Arizona and Virginia, **not in Texas**; availability zones only in Virginia [S19] |
| Azure Maps | ✅ | ✅ | ✅ | ✅ | ❌ | Per-feature Gov parity not verified (?) |
| Key Vault | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Azure Monitor (incl. App Insights, Log Analytics) | ✅ | ✅ | ✅ | ✅ | ✅ | Same features in Gov as Commercial [S1] |
| Data Explorer (ADX) | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Container Registry | ✅ | ✅ | ✅ | ✅ | ✅ | |
| Entra ID (Free / P1 / P2) | ✅ | ✅ | ✅ | ✅ | ✅ | |

**Compliance framing** [S2]:
- **Commercial Azure (US regions):** FedRAMP High P-ATO and DoD IL2. Commercial-only scope lists Container Apps at FedRAMP High + IL2 as well.
- **Azure Government (US Gov Arizona, Texas, Virginia):** FedRAMP High, IL2, IL4, and IL5. IL5 in these regions needs extra isolation configuration ("IL5 Workload Isolation").
- **Azure Government Secret:** IL6.
- **Azure Government Top Secret:** ICD 503.

Gov adds contractual US data residency and screened-US-person access commitments on top of FedRAMP High, which both clouds hold [S1].

**Implication:** keep worker code as plain OCI containers with a queue-pull loop. That way the same image runs on Container Apps jobs (Commercial dev, and Gov at FedRAMP High) or on AKS + KEDA (if IL4/5 is required), with no rewrite.

---

## 2. Large uploads (bytes → 50 GB GLBs)

### 2.1 Block blob limits (service version 2019-12-12+) [S5]

| Limit | Value |
|---|---|
| Max block size (Put Block) | 4000 MiB |
| Max committed blocks per blob | 50,000 |
| Max blob size (Put Block List) | ~190.7 TiB |
| Max single-shot Put Blob | 5000 MiB |
| Max uncommitted blocks | 100,000 |
| Uncommitted block lifetime | Discarded 1 week after last successful block upload if never committed |
| Block IDs | Equal length within a blob; base64 of ≤64 bytes |

Other block-blob properties [S5]:
- Blocks can be uploaded **in parallel, in any order**, and each can carry its own MD5.
- The commit (Put Block List) is atomic: if any listed block is missing, the whole commit fails.
- A commit discards any uncommitted blocks it doesn't list.

**50 GB sizing example:** 64 MiB blocks give about 800 blocks; 256 MiB blocks give about 200. Both are far under 50,000. Smaller blocks make retries cheaper; larger blocks reduce request count.

### 2.2 Throughput ceilings (standard GPv2) (DS) [S23]
- **US Gov Arizona and US Gov Virginia:** 60 Gbps ingress, 200 Gbps egress, and 40k requests/s per account.
- **Other regions (e.g., US Gov Texas):** 25 Gbps ingress and 20k requests/s.
- Throttling shows up as 503/500 errors; retry with exponential backoff.

### 2.3 Browser direct-to-blob with user delegation SAS [S6]

**Flow:**
1. The SPA calls `POST /uploads` with name, size, type, and checksum.
2. The API authorizes the user and creates an `upload` record (status `pending`).
3. The API uses its **managed identity** to get a **user delegation key** (Get User Delegation Key).
4. The API mints a SAS:
   - scoped to **one blob path** (e.g., `landing/{uploadId}/{filename}`)
   - permissions `c`/`w` only (create/write)
   - expiry measured in hours
5. The browser stages blocks in parallel and calls `commitBlockList`.
6. The `BlobCreated` event drives everything after that. The API never touches the bytes.

**Facts:**
- The identity requesting the key needs `.../blobServices/generateUserDelegationKey`, assigned at the storage account, resource group, or subscription scope. Storage Blob Data Reader, Contributor, and Owner all include it. [S6]
- The key is valid for **up to 7 days**, and so is any SAS signed with it. [S6]
- **Revocation:** call Revoke User Delegation Keys, or remove the RBAC assignment. This is much better than account-key SAS. [S6]
- **User-bound SAS:** with `sv` 2025-07-05 or later, the `sduoid` field binds the SAS to one Entra user's `oid`. The caller must then also present a bearer token whose `oid` matches. (DS, new) This gives defense-in-depth, but the browser would then need a storage-scoped token, which adds MSAL scope complexity. Optional. [S6]
- **Auditing:** `saoid`/`suoid` record the end user's object ID in storage diagnostic logs. Using them requires extra RBAC actions (`runAsSuperUser` / `manageOwnership`). [S6]
- In Azurite there is no Entra (?) — see §6. In dev, use an account-key SAS behind the same interface.

### 2.4 CORS on the storage account [S7]
- CORS is off by default and is configured per service (Blob).
- Up to **5 rules** per service, 2 KiB total, and up to 256 characters per origin or header.
- Allow the SPA origin(s); methods `PUT, GET, HEAD, OPTIONS`; headers `x-ms-*`, `content-type`, `content-md5`; expose `x-ms-*`, `etag`.
- Set a long `MaxAgeInSeconds`, because successful preflights are billed.
- CORS is **not** auth; the SAS is.
- CORS is not supported on **premium** GPv1/v2 accounts.

### 2.5 JS SDK (`@azure/storage-blob`) [S8]
- **Upload methods:**
  - `uploadData` works in the browser (Buffer, Blob, ArrayBuffer).
  - `uploadFile` and `uploadStream` are Node-only.
- **`BlockBlobParallelUploadOptions`:** `blockSize`, `concurrency`, `maxSingleShotSize` (default 256 MiB), `tier`, `tags`.
- **Checksums:** the SDK supports CRC64-NVME transfer validation (`uploadContentChecksumAlgorithm: "StorageCrc64"`). (DS, new)
- **Concurrency:** the SDK doesn't support concurrent writers to the same blob [S8], which is fine with one uploader per upload ID.
- **Resumability:** `uploadData` is not resumable across page reloads. For multi-GB files, write a thin uploader:
  1. Slice the `File` with `file.slice()`.
  2. Compute deterministic block IDs (e.g., `base64(pad(index, 6))`).
  3. Call `stageBlock` with a concurrency pool.
  4. Persist `{uploadId, blockSize, completedBlocks}` in IndexedDB.
  5. On resume, call `getBlockList("uncommitted")` and skip blocks already staged.
  6. Call `commitBlockList` with the ordered list.

  Get a fresh SAS from the API when the old one expires. The 7-day uncommitted window bounds how long an upload can stay paused [S5]. (Design inferred from [S5][S8].)

### 2.6 tus vs native block upload [S24]

| | tus 1.0 | Azure Put Block / Put Block List |
|---|---|---|
| Resume | `HEAD` → `Upload-Offset`, then `PATCH` | Get Block List (uncommitted), then re-stage missing blocks |
| Parallel | Needs the Concatenation extension | Native (any order) |
| Checksums | Optional extension (SHA1 required) | Per-block MD5 / CRC64 |
| Server | **A tus server must receive the bytes**, so you run tusd plus storage plus egress | Blob Storage *is* the server |
| Portability | Provider-neutral protocol | Azure-specific (the S3 multipart concept is analogous) |

**Recommendation:** native blocks. tus would put a byte-proxying service back into the path, which is the thing we're trying to avoid. For OIDC/provider portability, keep the abstraction at the API level: `createUpload` returns `{strategy: "azure-block", url, sas, blockSize}`. A future S3 or tus strategy can then plug in behind the same interface.

---

## 3. Event-driven ingestion

### 3.1 BlobCreated → Event Grid → queue → workers

**Event filtering** [S9]:
- Filter by subject prefix/suffix: `/blobServices/default/containers/landing/`.
- Filter `data.api` in `PutBlockList`, `PutBlob`, `CopyBlob`, `FlushWithClose`. Those calls fire `BlobCreated` only after the data is fully committed; `FlushWithClose` is the ADLS/dfs path.

**Delivery semantics** [S9][S10]:
- Delivery is **at-least-once**, **without ordering guarantees**, and has no latency SLA (minutes are possible).
- Use `sequencer` to order events per blob, and `eTag` to detect stale events.
- Check `topic` and `eventType`; don't assume them.

**Retry and dead-lettering** [S10]:
- **Defaults:** 30 attempts, 1440-minute TTL.
- **Backoff:** exponential, from 10 s up to 12 h.
- **Dead-lettering:** off by default. Enable it to a blob container, or events get dropped on 400/413 errors.
- **Batching:** up to 5,000 events per batch, all-or-none.

**Queue target** [S10][S25]:
- Route to a **Service Bus queue**, a supported Event Grid destination with custom delivery properties.
- Service Bus gives peek-lock, dead-letter queue, duplicate detection, and sessions.
- **Message size:** Standard tier 256 KB; Premium tier up to 100 MB via AMQP. Events are small, so Standard is fine.
- Storage Queue is a cheaper alternative with fewer semantics.

**Worker contract** (design):
1. Receive the message.
2. Upsert the `upload` row keyed by blob URL + eTag. Exit if it's already processed.
3. Stream from blob. Never assume the file fits in memory.
4. Write outputs to `processed/` or `derived/`.
5. Complete the message.

### 3.2 Compute: Functions vs Container Apps jobs vs AKS

| | Functions Flex Consumption [S11][S12] | Functions Premium / Dedicated [S11] | Container Apps jobs [S3][S13][S26] | AKS + KEDA |
|---|---|---|---|---|
| Max memory | **4 GB** (sizes 512 MB / 2 GB / 4 GB) | 3.5–14 GB (Premium) | Consumption: 4 vCPU / 8 GiB per replica; Dedicated D up to 32 vCPU / 128 GiB, E up to 32 vCPU / 256 GiB | Any VM SKU |
| Temp disk | **0.8 GB** | 11–61 GB (Premium) | Not stated in profile doc (?) — mount Azure Files or stream | Node disk / PVC |
| Timeout | Unbounded, but 60-minute grace on scale-in; HTTP 230 s | Unbounded (Premium); Dedicated needs Always On | `replicaTimeout` (s), no max stated (?) | None |
| Scale | ≤1000 instances; 250-core regional quota (default) | 20–100 | KEDA ScaledJob; event job per message; managed identity on Service Bus / Queue scalers | KEDA |
| Gov compliance | IL2–IL6 (Functions overall) | IL2–IL6 | **FedRAMP High / IL2 only** [S2] | IL2–IL6 [S2] |
| Fit | Small-file handlers (IoT JSON, CSV < ~500 MB) | OK middle ground | **Best default for GLB jobs** | IL4/5 or heavy GPU |

Other notes:
- Linux Consumption (legacy) retires 2028-09-30, and v3 on Linux Consumption stops 2026-09-30 (DS) [S11]. Flex Consumption is the recommended serverless plan, and its Blob trigger is Event Grid–based only [S12].
- Container Apps jobs: platform maintenance can interrupt long replicas. Set `replicaRetryLimit` ≥ 1 and design for at-least-once processing [S3].
- **Event-job pattern:** use one job execution per message (`azure-servicebus` scaler, `messageCount=1`) with KEDA polling at a 30 s default [S3][S26]. Jobs don't support Dapr or ingress [S3].
- **GLB processing** means validate, extract metadata, generate a thumbnail, and optionally decimate or convert to 3D Tiles. It needs 16–64 GiB of RAM for large meshes (estimate (?)), so use a **Dedicated E-series workload profile** for the heavy job and the Consumption profile for light jobs.

### 3.3 Tiers, lifecycle, and container layout

**Access tiers** [S27]:

| Tier | Min retention (early-deletion charge) | Latency | Notes |
|---|---|---|---|
| Hot | — | ms | Default for new GPv2 accounts |
| Cool | 30 days | ms | 99% availability |
| Cold | 90 days | ms | JS SDK ≥ 12.13 |
| Archive | 180 days | **hours** (rehydrate up to 15 h) | Offline; LRS/GRS/RA-GRS only (no ZRS/GZRS); lifecycle policies can't rehydrate |

"Smart tier" auto-moves blobs between hot, cool, and cold (DS, new) [S27]. Lifecycle tiering and delete policies work on both flat and HNS accounts [S18].

**Proposed layout** (one account per environment; separate accounts to isolate RBAC and lifecycle are an option):

| Container | Contents | Lifecycle |
|---|---|---|
| `landing/{uploadId}/…` | Raw uploads. Write-only SAS. Event source. | Delete or move after successful promote. Uncommitted blobs self-expire in 7 days [S5]. |
| `raw/{dataset}/{yyyy}/{mm}/{uploadId}/…` | Immutable originals | Cool after 30 d → Cold after 90 d → Archive (if allowed) after 180 d. Consider container-level WORM. |
| `processed/…` | Normalized outputs (e.g., Draco/meshopt GLB, COG, GeoParquet) | Hot → Cool |
| `derived/…` | Thumbnails, tiles, previews | Hot. Served via short-lived read SAS or CDN. |
| `quarantine/…` | Failed validation or malware-scan hits | Retain N days |

### 3.4 ADLS Gen2 hierarchical namespace (HNS): pros and cons [S18][S28]

| Pros | Cons |
|---|---|
| Atomic directory rename/delete (commit-by-rename for Spark/Hive) | **Irreversible** once enabled |
| POSIX ACLs (and the `suoid` ACL check on SAS) | **Not supported with HNS:** blob versioning, change feed, object replication, point-in-time restore, version-level WORM, customer-provided keys |
| Better for analytics engines (Synapse, Databricks, Fabric) | Blob index tags and snapshots are **preview only** |
| Same Blob APIs and Event Grid events still work | **Azurite doesn't emulate HNS** [S20], which breaks dev parity |

**Recommendation:**
- Use a **flat-namespace GPv2** account for `landing`/`raw`/`processed`/`derived`. That keeps versioning, change feed, and version-level immutability for originals, plus Azurite parity.
- If an analytics lake (GeoParquet for Spark/Fabric) is added later, create a **separate HNS account** for `lake/`.

---

## 4. Routing tiny IoT/bio readings

Readings range from bytes to KB and arrive at high count and high frequency. Don't store each one as a blob: that's per-object cost, and each blob fires an event.

| Option | Strengths | Weaknesses | Gov |
|---|---|---|---|
| **Postgres Flex** (+ TimescaleDB 2.24 or pg_partman 5.4.3, PostGIS 3.6.1) [S15] | One DB for metadata, spatial, and readings. SQL joins to assets. Time partitioning. pgvector in the same DB. | Write throughput bounded by a single primary. Hot-table bloat/vacuum. ZR-HA only in Gov Virginia [S14]. | IL2–IL6 [S2] |
| **Azure Data Explorer** [S29] | Built for telemetry and time series. "Millions of events per second" ingest. KQL time-series and geospatial functions. Continuous export to ADLS. | Another engine and language (KQL; T-SQL subset supported). Cluster cost floor. | IL2–IL6 [S2] |
| **Cosmos DB** | Global, low-latency point reads/writes, TTL | RU cost for analytics scans; weak for time-window aggregation (?) (not verified this session) | IL2–IL6 [S2] |
| **Parquet in ADLS** (micro-batched) | Cheapest at rest; lake-friendly | Needs a batching writer (no per-row writes); query engine needed | via Storage |

**Recommendation (portfolio scale):**
- Ingest via the API (or an IoT/HTTP endpoint) into **Postgres partitioned tables**: TimescaleDB hypertable or pg_partman by day, with PostGIS `geometry(Point, 4326)`.
- Keep the routing decision behind a `ReadingSink` interface, with ADX as the documented "scale-out" path.
- Batch files of readings (CSV/JSONL uploads) go through the same blob pipeline, and a worker bulk-`COPY`s them into Postgres.
- Archive cold partitions to Parquet in `raw/`. The `azure_storage` extension (1.9) can export or import blobs directly from Postgres [S15].
- **Extension caveats:**
  - `h3` is **not** in the Flexible Server extension list [S15]. Use PostGIS or compute H3 cells app-side.
  - TimescaleDB, pg_partman, pg_cron, and azure_storage need `shared_preload_libraries` [S15].

---

## 5. Auth: OIDC (Entra ID), RBAC, managed identity

### 5.1 SPA + API
- **Flow:** authorization code + PKCE via `@azure/msal-browser` / `msal-react`, with redirect URIs registered under the **"Single-page application"** platform type [S30]. The MSAL docs list no implicit flow for this scenario. Not re-verified this session: SPA refresh tokens are limited to 24 h (?) and implicit is deprecated (?).
- **Registrations:** two app registrations.
  - **API** registration: exposes a scope (e.g., `api://ingest/Access`) and **defines the app roles**.
  - **SPA** registration: requests that scope.
  - Roles assigned on the API registration appear in the access token's `roles` claim [S31].
- **Gov authority:** `https://login.microsoftonline.us/{tenantId}`, with the app registered in `portal.azure.us` [S16][S17].
  - **Nuance:** Azure Government apps may authenticate users from an **Entra Public** tenant (common for GCC orgs) or an **Entra Government** tenant (GCC High/DoD). You must register the app in whichever tenant you choose [S17]. **Make the authority host and tenant pure configuration.**

### 5.2 Authorization model

**App roles vs groups** [S31][S32]:
- **Use app roles** (e.g., `Ingest.Reader`, `Ingest.Uploader`, `Ingest.Admin`), assigned to security groups in Enterprise Applications. App roles move with the app; group IDs differ per tenant [S31].
- **App role limit:** 700 role + scope definitions per app [S31].
- **Groups overage:** the `groups` claim is replaced with an overage pointer above **200 groups (JWT)**, which forces a Graph call. App roles avoid this [S32].
- **Gotcha:** a service principal added to a group that holds an app role does **not** get the `roles` claim [S31]. Assign app roles to managed identities directly.

**API validation** [S32]:
- Check `iss` (tenant), `aud` (API client ID), `exp`/`nbf`, and the signature via JWKS.
- Key users on `oid` (immutable, per tenant) + `tid`, not on `preferred_username` or `name` (mutable).

### 5.3 Keeping it OIDC-generic

| Generic (any OIDC provider) | Entra-specific (isolate behind an adapter) |
|---|---|
| Discovery `/.well-known/openid-configuration`, JWKS, `iss`/`aud`/`exp` validation, auth code + PKCE | `roles` claim from app roles; `scp` claim name; `oid`/`tid` |
| Configurable claim → role mapping (`roles`, `groups`, `realm_access.roles` for Keycloak, etc.) | Groups overage handling via Graph [S32] |
| Standard SPA library option: `oidc-client-ts` (?) (not evaluated here) instead of MSAL | National cloud authority hosts [S16] |
| | **User delegation SAS** requires the API's *own* Entra identity, not the user's IdP, so storage auth stays Azure-native regardless of user IdP [S6] |

**Design:**
- The API config holds `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `ROLE_CLAIM_PATH`.
- Default `ROLE_CLAIM_PATH` is `roles`. A Keycloak profile would use `realm_access.roles`.
- In dev, a local Keycloak or mock OIDC server (?) can stand in for Entra. Not researched here; see the auth research doc if any.

### 5.4 Managed identity for services
- **API:** a user-assigned managed identity with Storage Blob Data Contributor (or a narrower role plus `generateUserDelegationKey`) on the account [S6]. It also gets Service Bus Data Sender if needed, Key Vault Secrets User, and Entra auth to Postgres (?). Flexible Server supports Entra auth; that wasn't re-verified this session.
- **Workers:** a managed identity for blob read/write and Service Bus receive. KEDA scale rules in Container Apps authenticate via `identity: 'system'` or a user-assigned ID. This works for Service Bus, Storage Queue, and Event Hubs scalers [S26].
- **Terraform:** set `storage_use_azuread = true` so data-plane calls avoid shared keys [S33]. Then disable shared-key access on the account (?).

---

## 6. Local dev parity

| Cloud component | Local stand-in | Gaps |
|---|---|---|
| Blob Storage | **Azurite** (Blob, Queue, Table) [S20] | **No ADLS Gen2/HNS, no Files** [S20]. IP-style URLs (`127.0.0.1:10000/devstoreaccount1/...`) unless hosts-file "production-style" URLs are used. Not for perf testing. Entra/OAuth and user delegation SAS parity not verified (?) — plan on account-key SAS in dev behind the same `SasIssuer` interface. Legacy Storage Emulator capped blobs at 2 GiB [S5]; Azurite limit unknown (?). |
| Event Grid | **No official Microsoft emulator** found (?). Community simulators exist (e.g., AzureEventGridSimulator) — not primary sources. | **Practical approach:** a local "blob-event shim". Either poll Azurite's list-blobs, or have the dev uploader call a `/dev/blob-created` endpoint, then publish an Event Grid–schema message to the Service Bus emulator. The worker sees identical messages. |
| Service Bus | **Official Service Bus emulator** (Linux Docker) [S21] | No Entra auth, no VNet, no partitioned entities, AMQP TCP only (no WebSockets), **no persistence across restarts**. 1 namespace, 50 entities, 256 KB messages, 1 h TTL default. Configured by `config.json`. No SLA or official support [S21]. |
| Postgres Flex | `postgis/postgis` image + `pgvector` (+ timescaledb) in Docker | Pin major versions to match Flex (PostGIS 3.6.x, pgvector 0.8.x) [S15]. Azure-only extensions (`azure_storage`, `azure_ai`, `pg_diskann`) unavailable locally. |
| Entra ID | Mock OIDC server or Keycloak (?) | Claim shape differs; use the role-claim mapping (§5.3) |
| Container Apps jobs | `docker compose` worker with a pull loop | KEDA scaling not exercised locally |

---

## 7. Infra as code: Bicep vs Terraform

| | Bicep [S34] | Terraform azurerm [S33] |
|---|---|---|
| Gov support | ARM-native; target Gov by deploying with the CLI set to `AzureUSGovernment` [S1] | `environment = "usgovernment"` (or `ARM_ENVIRONMENT`); also `public`, `china`, `german`; `metadata_host` alternative |
| New resource types / API versions | **Immediate** ("day zero") | Waits for provider releases (?) (not stated in source; general knowledge) |
| State | None. Azure is the state; `what-if` for preview. | State file (remote backend needed) |
| CI auth | Azure CLI / OIDC federated credentials | `use_oidc` (`ARM_USE_OIDC`), `use_msi`, `use_cli` (default true) |
| Multi-cloud / non-Azure (Entra app regs, Keycloak, DNS elsewhere) | Azure-only; Entra app registrations need the Microsoft Graph Bicep extension (?) | One tool via `azuread` and other providers |
| Data-plane auth | n/a | `storage_use_azuread` (default false; requires Storage RBAC roles) |

**Recommendation:**
- **Terraform** if the portfolio wants to show cloud-agnostic skills, or to manage Entra app registrations and app roles in the same codebase (`azuread` provider).
- **Bicep** if it's Azure-only and Gov-first with the newest API versions (e.g., Container Apps job properties).
- Either way, a single `cloud` variable drives the endpoints. Verify that each resource type and API version you use is available in Gov regions before relying on it.

---

## Open questions / to verify in a real Gov subscription
1. Container Apps region availability in Azure Government. Check Products-by-region or `az provider show -n Microsoft.App --query "resourceTypes[?resourceType=='managedEnvironments'].locations"` with the cloud set to AzureUSGovernment.
2. Flex Consumption locations in Gov (`az functionapp list-flexconsumption-locations`).
3. Postgres Flex extension allow-list parity in Gov (`az postgres flexible-server parameter show --name azure.extensions`).
4. Azurite's support for user delegation SAS and OAuth, and its max blob size.
5. Azure Maps per-API parity in Gov (rendering, search, Creator).

---

## Sources

| ID | URL | Page date / updated |
|---|---|---|
| S1 | https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure | ms.date 2025-08-21; updated 2026-08-18 |
| S2 | https://learn.microsoft.com/en-us/azure/azure-government/compliance/azure-services-in-fedramp-auditscope | ms.date 2026-09-21; tables "Last updated: February 2026" |
| S3 | https://learn.microsoft.com/en-us/azure/container-apps/jobs | 2026-09-16 |
| S5 | https://learn.microsoft.com/en-us/rest/api/storageservices/understanding-block-blobs--append-blobs--and-page-blobs | 2021-04-01 / updated 2023-06-27 |
| S6 | https://learn.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas | updated 2026-09-15 |
| S7 | https://learn.microsoft.com/en-us/rest/api/storageservices/cross-origin-resource-sharing--cors--support-for-the-azure-storage-services | 2023-01-31 |
| S8 | https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-upload-javascript | 2025-03-25 / updated 2026-10-09 |
| S9 | https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-event-overview | 2024-04-24 |
| S10 | https://learn.microsoft.com/en-us/azure/event-grid/delivery-and-retry | 2026-02-09 |
| S11 | https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale | 2026-09-16 |
| S12 | https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan | 2026-09-15 |
| S13 | https://learn.microsoft.com/en-us/azure/container-apps/workload-profiles-overview | 2025-11-04 / updated 2026-06-26 |
| S14 | https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/overview (redirects to /postgresql/overview) | 2026-01-09 / updated 2026-09-05 |
| S15 | https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions | 2026-07-10 |
| S16 | https://learn.microsoft.com/en-us/entra/identity-platform/authentication-national-cloud | 2025-01-15 |
| S17 | https://learn.microsoft.com/en-us/entra/identity-platform/msal-national-cloud | 2021-09-21 / updated 2026-06-15 |
| S18 | https://learn.microsoft.com/en-us/azure/storage/blobs/storage-feature-support-in-storage-accounts | 2026-05-18 |
| S19 | https://learn.microsoft.com/en-us/azure/search/search-region-support | 2026-10-23 |
| S20 | https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite | 2025-08-25 |
| S21 | https://learn.microsoft.com/en-us/azure/service-bus-messaging/overview-emulator | 2026-09-18 |
| S22 | (Search found no learn.microsoft.com Event Grid emulator; community: https://github.com/pm7y/AzureEventGridSimulator — non-primary) | — |
| S23 | https://learn.microsoft.com/en-us/azure/storage/common/scalability-targets-standard-account | 2025-10-31 / updated 2026-10-16 |
| S24 | https://tus.io/protocols/resumable-upload | protocol 1.0.0 (2016-03-25) |
| S25 | https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-quotas | 2026-10-13 |
| S26 | https://learn.microsoft.com/en-us/azure/container-apps/scale-app | 2026-05-19 |
| S27 | https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-overview | 2026-04-02 |
| S28 | https://learn.microsoft.com/en-us/azure/storage/blobs/data-lake-storage-namespace | 2026-07-02 |
| S29 | https://learn.microsoft.com/en-us/azure/data-explorer/data-explorer-overview | 2026-07-23 |
| S30 | https://learn.microsoft.com/en-us/entra/identity-platform/scenario-spa-app-configuration | 2025-05-12 |
| S31 | https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps | 2026-09-25 |
| S32 | https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference | 2023-05-26 / updated 2026-07-24 |
| S33 | https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/index.html.markdown | main branch, fetched 2026-10-24 |
| S34 | https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/overview | 2026-01-08 |
