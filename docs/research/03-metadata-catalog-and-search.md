# 03 — Metadata Catalog & Search Architecture

Researched 2026-10-24 from primary sources (specs, official docs, project repos). Every claim links to its source. **[UNVERIFIED]** marks things I inferred or could not confirm in a primary source.

## TL;DR

1. **Use STAC as the external contract.** Run **pgSTAC + stac-fastapi-pgstac** on **Azure Database for PostgreSQL Flexible Server** (Azure Gov). It's the stack behind Microsoft Planetary Computer and eoAPI, and every extension it needs (PostGIS, pg_trgm, pgvector, pg_diskann, btree_gist, pg_partman, pg_cron) is on Azure's supported list.
2. **pgSTAC's built-in free text is weak.** It's unindexed and slow at item level. Add a platform-owned search table next to pgSTAC (tsvector+GIN, pg_trgm, pgvector HNSW, facet columns) and fuse its rankings with RRF in SQL.
3. **Azure AI Search can't be the spatial engine.** Index fields are **points only** (`Edm.GeographyPoint`); polygons work only as query constants. File footprints are polygons. It also costs a second store and a sync pipeline. Use it only if its semantic ranker turns out to be worth that.
4. **Hold OpenSearch/Elasticsearch in reserve.** They have the best text, facet and geo_shape support, but I found no managed offering in Azure Gov, so you'd self-host on AKS. An outbox/CDC feed keeps this option cheap to add later.
5. **Semantic search is feasible in Gov.** `text-embedding-3-small` (1536-d) is available in Azure Gov and fits under pgvector's 2,000-dim HNSW limit for `vector`.

---

## 1. STAC API spec & implementations

### 1.1 Spec status

- STAC API **v1.0.0** is the latest release. It is built from Core, OGC API-Features and Item Search, and extensions live in the separate stac-api-extensions org. The README still carries a "beta" stability note. https://github.com/radiantearth/stac-api-spec

| Extension | Maturity | What it gives us | Source |
|---|---|---|---|
| Filter (CQL2) | Candidate | Text and JSON filter languages; `/queryables`; optional advanced comparison (LIKE/BETWEEN/IN), spatial (DE-9IM), temporal, array and case-insensitive classes | https://github.com/stac-api-extensions/filter |
| Sort | Stable | `sortby` | https://stac-api-extensions.github.io/ |
| Fields | Candidate | include/exclude projection | https://stac-api-extensions.github.io/ |
| Transaction | Candidate | POST/PUT/PATCH/DELETE of items and collections | https://stac-api-extensions.github.io/ |
| Free-text (`q`) | **Proposal** | Basic (OR-ed terms) and advanced (phrases, AND/OR, +/-) syntax. The fields searched are left to the implementation | https://github.com/stac-api-extensions/freetext-search |
| Collection Search | Candidate | Search over collections | https://stac-api-extensions.github.io/ |
| Aggregation | Proposal | Facet-style counts | https://stac-api-extensions.github.io/ |
| Context | Deprecated | (replaced by `numberMatched`) | https://stac-api-extensions.github.io/ |

Filter details: `filter-lang` defaults to cql2-text for GET and cql2-json for POST. Both `/queryables` and `/collections/{id}/queryables` are required. https://github.com/stac-api-extensions/filter

**Implication:** STAC covers spatial, temporal, CQL2 attribute filters and sorting well. It covers *facets* (Aggregation, Proposal) and *free text* (Proposal) thinly, and has **nothing for semantic/vector search**. Expose those as a platform API alongside the STAC API. Don't bend STAC to carry them.

### 1.2 Implementations

| | stac-fastapi-pgstac | stac-fastapi-elasticsearch-opensearch (SFEOS) |
|---|---|---|
| Core framework | stac-fastapi 7.0.0 (Sep) https://github.com/stac-utils/stac-fastapi/releases | same base |
| Version / cadence | 7.1+ supports pgstac `>=0.9.2,<0.11` https://github.com/stac-utils/stac-fastapi-pgstac | v8.0.0 released 2026-10-23 (ETag/If-Match, sortables; breaking) https://github.com/stac-utils/stac-fastapi-elasticsearch-opensearch |
| Extensions | Sort "fully supported", Transactions (env flag), multi-tenant catalogs (optional). Filter, fields and free-text come from pgSTAC; not enumerated in that README **[UNVERIFIED]** | Collection search, catalogs, sortables, queryables, transactions, free text, aggregation, auth, rate limiting, Prometheus metrics |
| Backend versions | PostgreSQL ≥13, PostGIS ≥3 (3.1+ best), btree_gist https://stac-utils.github.io/pgstac/pgstac/ | ES 9.3.2 / OpenSearch 3.5.0 by default |
| Perf notes | Datetime sort is specially optimized. Sorting on other fields over millions of rows is slow; README suggests short statement timeouts or disabling Sort. Hydration can move to the API (`USE_API_HYDRATE`) | One index per collection by default. Optional datetime-partitioned indexes (needs Redis; 25 GB max per index; "large temporal collections with millions of items"). `COUNT_TIMEOUT` 0.5 s. Keep `FREE_TEXT_FIELDS` narrow at millions of items |

---

## 2. pgSTAC, Planetary Computer, eoAPI

### 2.1 pgSTAC internals (https://stac-utils.github.io/pgstac/pgstac/)

- Everything lives in the `pgstac` schema: `collections`, `items` (+ `items_staging`), `queryables`, `stac_extensions`, `pgstac_settings`. `partitions` and `partition_steps` are materialized views.
- **Partitioning:** by collection, then optionally by **year or month** via `collections.partition_trunc`. This must be set before loading. Guidance is to keep partitions **under a few hundred thousand rows**, and over-partitioning hurts too.
- **Queryables** drive which property indexes get built (`property_wrapper` = to_int/to_float/to_tstz/to_text, `property_index_type`). `missing_queryables()` suggests entries. Default indexes cover id, datetime, collection and geometry.
- **Counts:** context is off by default. `auto` uses estimated counts from statistics, because exact counts are "very expensive on large databases."
- **Dehydration:** since v0.5.0, item fields that duplicate collection-level metadata (e.g. `item_assets`) are stripped on ingest and merged back on read. https://github.com/stac-utils/pgstac/blob/main/CHANGELOG.md
- **Free text:** added in v0.9.2 as a `tsquery` over title, description and keywords. The changelog says it is **"unindexed and slow for item-level searches."** https://github.com/stac-utils/pgstac/blob/main/CHANGELOG.md
- Latest release is **v0.10.0** (Oct 1). https://github.com/stac-utils/pgstac/releases
- **Version-pin caveat:** titiler-pgstac's compatibility table tops out at pgstac `>=0.9,<0.10`. https://github.com/stac-utils/titiler-pgstac. Pin pgstac to 0.9.x until titiler-pgstac supports 0.10 **[verify at build time]**.

**Sizing for tens of millions of records:** choose collection granularity so each collection×month partition stays under a few hundred thousand rows. Since partitioning keys on collection, **collection = project or data product** fits better than collection = file format **[design judgment]**.

### 2.2 Microsoft Planetary Computer (reference architecture)

- **Uses pgSTAC: confirmed.** planetary-computer-apis has a `pgstac` dir. `pcstac` is a stac-fastapi implementation and `pctiler` is TiTiler using titiler-pgstac. The repo deploys to AKS (dev). The production deployment code isn't public, and the repo has no support for non-dev deployments. https://github.com/microsoft/planetary-computer-apis
- **Planetary Computer Pro (GeoCatalog)** is the managed "buy" option. It provides a managed STAC API, ingestion, tiling and WMTS, with access through Entra ID. https://learn.microsoft.com/en-us/azure/planetary-computer/microsoft-planetary-computer-pro-overview. It's available in **US Gov Virginia**, with `*.geocatalog.spatio.azure.us` endpoints. https://learn.microsoft.com/en-us/azure/planetary-computer/us-government-cloud-support. It is marketed for raster/data-cube data, and its fit for arbitrary vector or "any file" ingestion is **[UNVERIFIED]**. Worth an ADR as the "considered alternative."

### 2.3 eoAPI (Development Seed reference stack)

- Components: pgSTAC, stac-fastapi, titiler-pgstac, tipg. Deploys via AWS CDK or **Kubernetes Helm** (portable to AKS), and docs list a "STAC Auth Proxy." https://eoapi.dev/
- Recommendation: use eoAPI's Helm chart or composition as the starting skeleton and swap in Azure Gov PaaS Postgres.

---

## 3. Search engine options

### 3.1 Capability matrix

| Need | PostgreSQL (PostGIS + FTS + pg_trgm + pgvector) | OpenSearch / Elasticsearch | Azure AI Search | Meilisearch | Typesense |
|---|---|---|---|---|---|
| Polygon footprints indexed | Yes (PostGIS) | Yes: `geo_shape` tessellated to triangles in a BKD tree; point…multipolygon, envelope https://docs.opensearch.org/latest/mappings/supported-field-types/geo-shape/ | **No**: fields are `Edm.GeographyPoint` only; `Edm.GeographyPolygon` "can't be used in document fields" https://learn.microsoft.com/en-us/rest/api/searchservice/supported-data-types | Partial: `_geojson` (v1.22+), no holes, antimeridian must be split https://www.meilisearch.com/docs/capabilities/geo_search/how_to/use_geojson_format | `geopolygon` stores polygons, but only point-in-polygon queries are documented https://typesense.org/docs/latest/api/geosearch.html |
| Spatial predicates | Full DE-9IM, distance (geography) | INTERSECTS / DISJOINT / WITHIN / CONTAINS https://docs.opensearch.org/latest/query-dsl/geo-and-xy/geoshape/ | `geo.distance` (km), `geo.intersects` (point-in-polygon, CCW ring) https://learn.microsoft.com/en-us/azure/search/search-query-odata-geo-spatial-functions | radius / bbox / polygon overlap | radius / polygon |
| Full text | tsvector+GIN (stemming, ts_rank), no BM25 **[general PG knowledge]** | BM25, analyzers | BM25 + semantic ranker | typo-tolerant | typo-tolerant |
| Vector + hybrid | pgvector HNSW; RRF done by hand in SQL https://github.com/pgvector/pgvector | OpenSearch 2.19+ `score-ranker-processor` RRF https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/; ES RRF retriever GA (license tier unclear) https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion | Native hybrid with RRF (k=60), vector weighting, semantic rerank after RRF https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking | yes | yes |
| Azure Gov availability | Flexible Server in Gov (`postgres.database.usgovcloudapi.net`) https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure | **No first-party managed service found.** Elastic Cloud lists commercial Azure regions only https://www.elastic.co/docs/reference/cloud/cloud-hosted/regions → self-host on AKS **[UNVERIFIED that none exists]** | Gov Arizona, Texas, Virginia; semantic ranker in Arizona and Virginia only https://learn.microsoft.com/en-us/azure/search/search-region-support | self-host | self-host |
| Second store + sync | none | yes | yes | yes | yes |

Azure Postgres Flexible Server extension versions on PG16–18: postgis 3.6.1, vector 0.8.2, pg_diskann 0.6.5, pg_trgm 1.6, btree_gist, pg_partman 5.4.3, pg_cron 1.6, azure_ai. https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions. Installing pgSTAC on Flexible Server (it needs role creation and non-superuser grants) should work, since PC and eoAPI run on Postgres, but **[UNVERIFIED on Flex Server specifically — spike it]**.

### 3.2 Recommendation (single team, must scale to tens of millions)

**PostgreSQL-only, in two layers, plus an escape hatch:**

1. **`pgstac` schema** is the system of record for STAC Items, the source for spatial/temporal/CQL2 search, and what stac-fastapi and titiler serve.
2. **`catalog` schema** is platform-owned, with one row per item, kept in sync by a trigger or the ingest worker:
   - typed facet columns (`format`, `owner`, `project`, `tags text[]`, `datetime`, `geom`) with B-tree, GIN(`tags`) and GiST indexes
   - `search_tsv tsvector` (generated, weighted title > tags > description) with a GIN index, plus `pg_trgm` GIN on title for fuzzy matching and autocomplete
   - `embedding vector(1536)` with an HNSW index
3. **Facets:** run `GROUP BY` counts on the filtered set. Cap them, or use estimated counts at scale, following pgSTAC's own approach to counts.
4. **Escape hatch:** publish item change events through an outbox table to Service Bus. If text, facet or relevance load outgrows Postgres, an OpenSearch on AKS (or AI Search for text and vector only) can subscribe without changing ingest.

Why not lead with AI Search: point-only geo means footprint intersects queries go to PostGIS anyway, so every search becomes a two-engine join. Its standout feature (the semantic ranker) is available in Gov Arizona and Virginia, so it can be added later if wanted.

---

## 4. Semantic search

- **Embedding input:** a deterministic "metadata description" generated per item, e.g. `"{format} file '{title}' from project {project}, owner {owner}, covering {place/bbox}, {start}–{end}, tags: …, CRS …, {band/layer/schema summary}"`. Optionally add an LLM-written summary (gpt-4.1-mini is available in Gov, below). Store the text and hash it, and re-embed only when the hash changes.
- **pgvector:** HNSW params are `m` (16), `ef_construction` (64) and `hnsw.ef_search` (40). HNSW indexes cap at **2,000 dims for `vector`**, 4,000 for `halfvec`. Filters are applied *after* the ANN scan. With ef_search=40 and a filter matching 10% of rows you get about 4 results, so **use iterative scans (0.8.0+, `hnsw.iterative_scan`)** or partition and partial-index on hot filters. The README recommends RRF or a cross-encoder for hybrid search. https://github.com/pgvector/pgvector
- **pg_diskann** is also available on Azure Flex Server as an alternative ANN index. https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions
- **Hybrid ranking (RRF):** score = Σ 1/(k + rank), with k≈60. That's the same constant Azure AI Search and OpenSearch use (https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking). Implement it in SQL as two CTEs (FTS top-N, ANN top-N), a `FULL OUTER JOIN` on id, and a sum of reciprocal ranks, with the hard filters (bbox, time, facets) applied in both CTEs.
- **Azure OpenAI in Azure Government (verified):** endpoint suffix `openai.azure.us`. https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure. Embedding availability: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/azure-government

| Model (dims) | Standard: usgovarizona | Standard: usgovvirginia | Data Zone Standard (both) |
|---|---|---|---|
| text-embedding-3-small (1536) | ✅ | – | ✅ |
| text-embedding-3-large (3072) | ✅ | – | ✅ |
| text-embedding-ada-002 (1536) | ✅ | ✅ | – |

  Chat models in Gov include gpt-4.1, gpt-4.1-mini, gpt-4o, o3-mini, gpt-5.1 and gpt-5.6 (varies by deployment type). **Pick `text-embedding-3-small`**: 1536 dims fit `vector` HNSW. 3-large's 3,072 dims would need `halfvec` or the `dimensions` parameter. Same source.
- **Natural-language → filters:** an optional LLM step that turns "flood imagery near Houston from 2024" into a CQL2-JSON filter plus a `q` string, validated against `/queryables`. This is my design proposal, not sourced.

---

## 5. Metadata data model

STAC Item per ingested file (or per logical dataset version), with the file as the primary asset and derivatives (COG, thumbnail, PMTiles) as extra assets.

| Concern | Field / mechanism | Source |
|---|---|---|
| Core | `id`, `collection`, `geometry` (footprint, EPSG:4326), `bbox`, `datetime` or `start_datetime`/`end_datetime`, `title`, `description`, `keywords` | STAC spec via https://github.com/radiantearth/stac-api-spec |
| Platform fields | custom prefix e.g. `itw:owner`, `itw:project`, `itw:tags[]`, `itw:format`, `itw:ingest_id`, `itw:classification` | own extension (publish a JSON Schema) |
| Per-format detail | Existing STAC extensions where they fit (projection, raster, eo, table, pointcloud); otherwise `itw:format_meta` JSONB validated by a per-format JSON Schema | **[extension list from general STAC knowledge]** |
| Checksums / dedup | `file:checksum` (self-identifying **multihash**, hex), `file:size` on the asset. Unique index on checksum gives exact-dup detection (content addressing) | https://github.com/stac-extensions/file (v2.1.0, Stable) |
| Versioning | `version`, `deprecated`; links `latest-version`, `predecessor-version`, `successor-version` (RFC 5829) | https://github.com/stac-extensions/version (v1.2.0, Candidate) |
| Lineage summary | `processing:lineage`, `processing:software`, `processing:level`, `processing:datetime`, `processing:version` | https://github.com/stac-extensions/processing (v1.3.0, Candidate) |
| Full provenance | Edge table modeled on **W3C PROV** (Entity = file version, Activity = ingest/transform job, Agent = user or service), with `used`, `wasGeneratedBy`, `wasDerivedFrom`, `wasAttributedTo`, `wasAssociatedWith` and timestamps. Export as PROV-JSON or JSON-LD on demand | https://www.w3.org/TR/prov-o/ (W3C Rec, 2013-04-30) |
| User tags | Store in `catalog.item_tags` (with who and when) and denormalize into `itw:tags` for STAC/CQL2 `a_contains` | design |

Versioning model (design): a stable `dataset_id`, plus an immutable item per version (`{dataset_id}.v{n}`) with version links. "Latest" is a flag or view used by default search. A re-upload with the same checksum doesn't create a new version.

---

## 6. Map-browsing UI

| Layer | Tool | Notes | Source |
|---|---|---|---|
| Footprints, low zoom / millions of items | **Martin** (Rust) or **pg_tileserv** (Go) MVT from a PostGIS view or function over footprints | Martin auto-discovers tables and functions and also serves PMTiles/MBTiles. pg_tileserv function layers take `(z,x,y, …params)` → `bytea`, so facet filters can be pushed into the tile SQL | https://github.com/maplibre/martin, https://github.com/CrunchyData/pg_tileserv |
| Footprints with CQL2 filters | **tipg** (OGC Features + Tiles, CQL2 filter, PostGIS) | Same family as eoAPI, matches the STAC filter language | https://github.com/developmentseed/tipg |
| Result list / high zoom | STAC `/search` GeoJSON (paged) | — | stac-fastapi |
| Raster previews | **TiTiler** (`/cog`, `/stac`, `/mosaicjson`, OGC Tiles/WMTS) | thumbnails, previews and tiles on the fly from COGs | https://developmentseed.org/titiler/ |
| Mosaic of search results | **titiler-pgstac** (registered searches → dynamic mosaics) | watch the pgstac version pin (§2.1) | https://github.com/stac-utils/titiler-pgstac |

UI pattern (design): a MapLibre map with an MVT footprint layer filtered by the current facets. Bbox and draw-polygon tools feed CQL2 `s_intersects`. The facet sidebar shows counts, and the results list shows thumbnails, with "zoom to" and a TiTiler preview overlay. If the UI is STAC-browser based, it gets STAC Browser compatibility for free.

---

## Open questions / spikes

1. Install pgSTAC on Azure Postgres Flexible Server (Gov) and confirm role/privilege requirements.
2. Can titiler-pgstac run against pgstac 0.10, or does it need pinning to 0.9.x?
3. Benchmark the hybrid RRF query at 10M rows with filters, comparing iterative scan to pg_diskann.
4. Check whether Planetary Computer Pro handles non-raster files. If it does, document it as the managed alternative in an ADR.
5. Check the Elastic license tier for the RRF retriever, which only matters if Elasticsearch is chosen over OpenSearch.
