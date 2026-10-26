# Local stubs for what the vector recipes use of pyogrio. Anything not declared here is Any.
from collections.abc import Mapping
from typing import Any

__version__: str
__gdal_version_string__: str

def set_gdal_config_options(options: Mapping[str, str | int | bool | None]) -> None: ...
def __getattr__(name: str) -> Any: ...
