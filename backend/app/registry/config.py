"""Versioned, validated configuration for entity resolution and data-quality scoring.

Defaults live here. An operator may override them with a JSON file (``ATLAS_RESOLUTION_CONFIG_PATH`` /
``ATLAS_QUALITY_CONFIG_PATH``); the result is validated, hashed, and both ``version`` and ``hash`` are stored with
every run so each score is traceable to the exact configuration that produced it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import DomainValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ resolution


class EntityPoints(_Strict):
    """Evidence points added (positive) or removed (negative) for each signal. Scores are *similarity* scores."""

    name_exact: float = Field(0.40, ge=0, le=1)
    core_name_exact: float = Field(0.30, ge=0, le=1)
    name_similarity: float = Field(0.30, ge=0, le=1)  # scaled between name_similarity_min and 1.0
    legal_name_exact: float = Field(0.15, ge=0, le=1)
    alias_match: float = Field(0.25, ge=0, le=1)
    authoritative_id_match: float = Field(0.80, ge=0, le=1)
    country_match: float = Field(0.10, ge=0, le=1)
    country_conflict: float = Field(-0.20, ge=-1, le=0)
    address_similarity: float = Field(0.20, ge=0, le=1)
    address_conflict: float = Field(-0.10, ge=-1, le=0)
    category_match: float = Field(0.05, ge=0, le=1)
    type_match: float = Field(0.10, ge=0, le=1)
    type_conflict: float = Field(-0.15, ge=-1, le=0)
    owner_match: float = Field(0.15, ge=0, le=1)
    owner_conflict: float = Field(-0.15, ge=-1, le=0)
    geo_near: float = Field(0.25, ge=0, le=1)
    geo_far: float = Field(-0.25, ge=-1, le=0)
    manufacturer_match: float = Field(0.15, ge=0, le=1)
    manufacturer_conflict: float = Field(-0.10, ge=-1, le=0)
    code_match: float = Field(0.35, ge=0, le=1)  # product code schemes (SKU, MATERIAL_NUMBER, GTIN) equal
    uom_conflict: float = Field(-0.15, ge=-1, le=0)
    identifier_conflict: float = Field(-0.50, ge=-1, le=0)


class ResolutionConfig(_Strict):
    version: str = Field("rc-1", min_length=1, max_length=32)
    strong_threshold: float = Field(0.80, gt=0, le=1)
    possible_threshold: float = Field(0.55, gt=0, le=1)
    min_corroborating_families: int = Field(2, ge=1, le=6)
    name_similarity_min: float = Field(0.80, ge=0.5, lt=1)
    address_similarity_min: float = Field(0.85, ge=0.5, lt=1)
    generic_name_factor: float = Field(0.5, ge=0, le=1)
    geo_near_km: float = Field(0.5, gt=0, le=100)
    geo_far_km: float = Field(50.0, gt=0, le=20000)
    max_block_size: int = Field(60, ge=2, le=1000)
    max_pairs_per_run: int = Field(50_000, ge=1, le=5_000_000)
    max_candidates_per_run: int = Field(5_000, ge=1, le=100_000)
    max_runtime_seconds: float = Field(20.0, gt=0, le=600)
    authoritative_schemes: list[str] = Field(default_factory=lambda: ["LEI", "DUNS", "VAT", "GTIN", "TAX_ID"])
    code_schemes: list[str] = Field(default_factory=lambda: ["SKU", "MATERIAL_NUMBER", "GTIN"])
    # cross_country: how a country mismatch is treated per entity type: penalize | ignore
    cross_country: dict[str, str] = Field(
        default_factory=lambda: {"SUPPLIER": "penalize", "FACILITY": "penalize", "PRODUCT": "ignore"}
    )
    # Facilities in different countries are almost never the same site: stronger penalty multiplier.
    facility_country_conflict_multiplier: float = Field(1.5, ge=1, le=5)
    reopen_score_increase: float = Field(0.10, ge=0, le=1)
    points: EntityPoints = Field(default_factory=EntityPoints)

    @model_validator(mode="after")
    def _check(self) -> ResolutionConfig:
        if self.possible_threshold >= self.strong_threshold:
            raise ValueError("possible_threshold must be below strong_threshold")
        if any(v not in ("penalize", "ignore") for v in self.cross_country.values()):
            raise ValueError("cross_country values must be 'penalize' or 'ignore'")
        return self


# --------------------------------------------------------------------- quality


class FieldRule(_Strict):
    name: str
    weight: float = Field(1.0, gt=0, le=10)
    required: bool = True


class EntityQuality(_Strict):
    fields: list[FieldRule]
    recommended: list[str] = Field(default_factory=list)
    dimension_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "completeness": 0.25,
            "validity": 0.20,
            "consistency": 0.15,
            "uniqueness": 0.10,
            "freshness": 0.10,
            "traceability": 0.10,
            "reference_integrity": 0.10,
        }
    )


class QualityConfig(_Strict):
    version: str = Field("qc-1", min_length=1, max_length=32)
    fresh_days: int = Field(90, ge=1, le=3650)
    stale_days: int = Field(365, ge=2, le=7300)
    uniqueness_open_strong_penalty: float = Field(0.40, ge=0, le=1)
    uniqueness_open_possible_penalty: float = Field(0.15, ge=0, le=1)
    uniqueness_confirmed_penalty: float = Field(0.80, ge=0, le=1)
    low_score_threshold: float = Field(0.70, ge=0, le=1)
    entities: dict[str, EntityQuality] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> QualityConfig:
        if self.stale_days <= self.fresh_days:
            raise ValueError("stale_days must exceed fresh_days")
        for name, e in self.entities.items():
            if abs(sum(e.dimension_weights.values()) - 1.0) > 1e-6:
                raise ValueError(f"dimension_weights for {name} must sum to 1.0")
        return self


def default_quality() -> QualityConfig:
    return QualityConfig(
        entities={
            "SUPPLIER": EntityQuality(
                fields=[
                    FieldRule(name="name"),
                    FieldRule(name="country_or_region"),
                    FieldRule(name="category"),
                    FieldRule(name="status"),
                ],
                recommended=["legal_name", "address"],
            ),
            "FACILITY": EntityQuality(
                fields=[
                    FieldRule(name="name"),
                    FieldRule(name="facility_type"),
                    FieldRule(name="country_or_region"),
                    FieldRule(name="status"),
                ],
                recommended=["owner_supplier_id", "city", "address", "latitude", "capacity_value"],
            ),
            "PRODUCT": EntityQuality(
                fields=[
                    FieldRule(name="name"),
                    FieldRule(name="category"),
                    FieldRule(name="unit_of_measure"),
                    FieldRule(name="status"),
                ],
                recommended=["description", "manufacturer_supplier_id"],
            ),
        }
    )


# ---------------------------------------------------------------------- loading


def config_hash(model: BaseModel) -> str:
    blob = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load(path: Path | None, default: BaseModel, cls: type[BaseModel]) -> BaseModel:
    if path is None:
        return default
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON value must be an object")
        return cls.model_validate(_deep_merge(default.model_dump(mode="json"), raw))
    except (OSError, ValueError) as exc:
        raise DomainValidationError(f"Invalid configuration file '{Path(path).name}': {str(exc)[:300]}") from exc


def resolution_config(path: Path | None = None) -> ResolutionConfig:
    return _load(path, ResolutionConfig(), ResolutionConfig)  # type: ignore[return-value]


def quality_config(path: Path | None = None) -> QualityConfig:
    return _load(path, default_quality(), QualityConfig)  # type: ignore[return-value]
