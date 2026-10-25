# Sample corpus

`manifest.json` lists a realistic set of files to push through the platform, and what each must
become: its outcome and, once cataloged, its kind and metadata. No sample data is committed.

    make samples              # download, generate and verify the default set (~780 MB) into .cache/
    make samples LARGE=1      # plus the multi-GB stress files
    make up && make e2e       # upload every sample through the running stack and check it

Every download is pinned (a commit SHA, or a fixed object) and kept only if its sha256 matches.
Each entry cites its primary source and licence; all are public domain, CC0, CC-BY, MIT,
Apache-2.0 or, for Sentinel-2, the Copernicus data terms (free, attribution required: "Contains
modified Copernicus Sentinel data 2023"). Generated samples (stress meshes, sensor readings,
EICAR) and derived ones (a truncated COG) are made by `api/tools/samples.py`, deterministically.

The expectations in the manifest come from sources independent of the platform's readers: STAC
items, USGS metadata, and headers parsed by hand. Change them only when the source says so.
