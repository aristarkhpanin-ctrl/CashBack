"""HTTP clients for upstream services."""
from app.clients.recommendation_client import (
    CircuitBreaker,
    CircuitBreakerOpen,
    RecommendationClient,
)

__all__ = ["CircuitBreaker", "CircuitBreakerOpen", "RecommendationClient"]
