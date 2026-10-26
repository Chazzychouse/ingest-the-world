"""The tiler: map tiles of one raster item at a time, for previews (`python -m uvicorn
ingest_api.tiler.app:create_app --factory`).

It has no database and no login. A request carries a preview token the API minted (`previews`),
and the tiler serves the item the token names, from the blob it names, until it expires.
"""
