"""Единый нормативный набор контрактов (ARCH-07, CONTRACT-03)."""

from odin.contracts.common import (
    DEMO_PURCHASE_NOTICE,
    SCHEMA_VERSION,
    Availability,
    Fee,
    Price,
    PricePeriod,
    SourceReference,
    Validity,
)
from odin.contracts.offers import CandidateDestination, Hotel, HotelOffer, ReviewEvidence, TravelOffer
from odin.contracts.plan import (
    Cart,
    CartItem,
    ConnectionCheck,
    DemoReceipt,
    PlanComponent,
    ReplanEvent,
    TripPlan,
    TripState,
)
from odin.contracts.tools import AgentResult, SearchTermination, ToolRequest, ToolResult
from odin.contracts.trip import (
    Constraint,
    ConstraintEvaluation,
    ConstraintGroup,
    FallbackActivation,
    Party,
    Place,
    TripRequest,
)

# 17 типов ТЗ §7 + нормативные Price и Availability (CONTRACT-02).
NORMATIVE_MODELS = {
    "ToolRequest": ToolRequest,
    "ToolResult": ToolResult,
    "AgentResult": AgentResult,
    "TripRequest": TripRequest,
    "TripState": TripState,
    "TravelOffer": TravelOffer,
    "TripPlan": TripPlan,
    "Cart": Cart,
    "ReplanEvent": ReplanEvent,
    "SourceReference": SourceReference,
    "Constraint": Constraint,
    "ConstraintGroup": ConstraintGroup,
    "FallbackActivation": FallbackActivation,
    "ConstraintEvaluation": ConstraintEvaluation,
    "Hotel": Hotel,
    "HotelOffer": HotelOffer,
    "ReviewEvidence": ReviewEvidence,
    "Price": Price,
    "Availability": Availability,
}

# Дополнительные контракты проекта (не заменяют нормативные).
EXTRA_MODELS = {
    "SearchTermination": SearchTermination,
    "CandidateDestination": CandidateDestination,
    "ConnectionCheck": ConnectionCheck,
    "DemoReceipt": DemoReceipt,
    "CartItem": CartItem,
    "PlanComponent": PlanComponent,
    "Validity": Validity,
}

__all__ = [
    "DEMO_PURCHASE_NOTICE",
    "SCHEMA_VERSION",
    "NORMATIVE_MODELS",
    "EXTRA_MODELS",
    "Availability",
    "Fee",
    "Price",
    "PricePeriod",
    "SourceReference",
    "Validity",
    "CandidateDestination",
    "Hotel",
    "HotelOffer",
    "ReviewEvidence",
    "TravelOffer",
    "Cart",
    "CartItem",
    "ConnectionCheck",
    "DemoReceipt",
    "PlanComponent",
    "ReplanEvent",
    "TripPlan",
    "TripState",
    "AgentResult",
    "SearchTermination",
    "ToolRequest",
    "ToolResult",
    "Constraint",
    "ConstraintEvaluation",
    "ConstraintGroup",
    "FallbackActivation",
    "Party",
    "Place",
    "TripRequest",
]
