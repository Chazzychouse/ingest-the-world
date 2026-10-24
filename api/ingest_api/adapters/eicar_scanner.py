"""Development stand-in for malware scanning: flags the EICAR test file and nothing else.

In the cloud, Defender for Storage scans uploads. Locally this gives the quarantine path
(ING-4) something real to react to, using the industry's harmless test signature.
"""

from ingest_api.ports import BlobStore, ScanResult

# The EICAR test string, split so this source file doesn't itself trip a scanner.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR_SIGNATURE = "EICAR-Test-File"
# The test file is defined as starting the file, so the head is enough.
SCAN_BYTES = 4096


class EicarScanner:
    def __init__(self, store: BlobStore):
        self._store = store

    def scan(self, container: str, name: str) -> ScanResult:
        head = self._store.read_range(container, name, 0, SCAN_BYTES)
        if head.startswith(EICAR):
            return ScanResult(clean=False, signature=EICAR_SIGNATURE)
        return ScanResult(clean=True)
