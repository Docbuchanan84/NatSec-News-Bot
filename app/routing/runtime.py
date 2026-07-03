from __future__ import annotations

import os
from typing import Any

from app.models import AppConfig
from app.routing.config import RoutingConfigError, load_routing_config
from app.routing.engine import RoutingEngine
from app.routing_v2 import WeightedRoutingConfigError, WeightedRoutingEngine, load_weighted_routing_config


def selected_routing_engine_name(config: AppConfig) -> str:
    return os.environ.get("ROUTING_ENGINE") or config.settings.routing.engine


def load_selected_routing_engine(config: AppConfig) -> tuple[Any, Any, str]:
    engine_name = selected_routing_engine_name(config)
    if engine_name == "legacy":
        routing_config = load_routing_config(config.settings.routing.config_dir, config)
        return RoutingEngine(routing_config), routing_config, engine_name
    if engine_name == "weighted_v2":
        try:
            routing_config = load_weighted_routing_config(config.settings.routing.weighted_config_dir, config)
        except WeightedRoutingConfigError as exc:
            raise RoutingConfigError(exc.errors) from exc
        return WeightedRoutingEngine(routing_config), routing_config, engine_name
    raise RoutingConfigError([f"Unknown routing engine: {engine_name}"])
