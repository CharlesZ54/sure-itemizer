from __future__ import annotations

from typing import Callable

from ..config import Config
from .base import Source


def _amazon(cfg: Config) -> Source:
    from .amazon import AmazonSource

    return AmazonSource.from_config(cfg)


def _target(cfg: Config) -> Source:
    from .target import TargetSource

    return TargetSource.from_config(cfg)


def _ebay(cfg: Config) -> Source:
    from .ebay import EbaySource

    return EbaySource.from_config(cfg)


def _venmo(cfg: Config) -> Source:
    from .venmo import VenmoSource

    return VenmoSource.from_config(cfg)


# Register new merchants here: name -> factory(config).
REGISTRY: dict[str, Callable[[Config], Source]] = {
    "amazon": _amazon,
    "target": _target,
    "venmo": _venmo,
    "ebay": _ebay,
}


def build_sources(cfg: Config) -> list[Source]:
    unknown = [n for n in cfg.sources if n not in REGISTRY]
    if unknown:
        raise ValueError(f"Unknown source(s): {', '.join(unknown)}. Known: {', '.join(REGISTRY)}")
    return [REGISTRY[n](cfg) for n in cfg.sources]


__all__ = ["Source", "REGISTRY", "build_sources"]
