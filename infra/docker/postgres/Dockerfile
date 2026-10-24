# PostgreSQL 17 + PostGIS 3.6 + pgvector: the extension set Azure Flexible Server offers.
FROM postgis/postgis:17-3.6

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
