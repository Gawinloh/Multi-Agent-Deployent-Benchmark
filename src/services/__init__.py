"""Pluggable service definitions (L1 of the extensibility plan).

A *service* is a first-class plugin: schema + renderer + CIS checker +
benchmark + smoke test + compose fragment + corpus tag. This package
holds the registry that maps a service name to those pieces; the pieces
themselves live where they always have (``src/schemas``,
``src/validator/cis_checks``, ``src/validator/benchmarks``).

Call sites that used to name postgres, nginx and redis explicitly now
iterate :func:`src.services.catalog.for_spec`, so a service that is not
selected simply does not appear.
"""

from src.services.catalog import CATALOG, all_services, for_spec, get
from src.services.definition import ResourceShare, ServiceDefinition

__all__ = [
    "CATALOG",
    "ResourceShare",
    "ServiceDefinition",
    "all_services",
    "for_spec",
    "get",
]
