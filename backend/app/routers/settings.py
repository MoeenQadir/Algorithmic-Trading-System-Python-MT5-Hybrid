from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any

from app.models.database import get_db, SystemSettings
from app.config import get_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/", response_model=Dict[str, Any])
def get_settings_values(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Get all system settings."""
    settings = db.query(SystemSettings).all()
    return {s.key: s.value for s in settings}


@router.put("/", response_model=Dict[str, Any])
def update_settings(settings_data: Dict[str, Any], db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Update system settings."""
    for key, value in settings_data.items():
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            setting.value = str(value)
        else:
            db.add(SystemSettings(key=key, value=str(value)))
    db.commit()
    return {"status": "success", "updated": len(settings_data)}


@router.get("/defaults", response_model=Dict[str, Any])
def get_default_settings() -> Dict[str, Any]:
    """Get default settings from configuration."""
    settings = get_settings()
    return {
        "max_risk_per_trade": settings.max_risk_per_trade,
        "max_daily_loss": settings.max_daily_loss,
        "max_consecutive_losses": settings.max_consecutive_losses,
        "max_open_positions": settings.max_open_positions,
        "min_risk_reward": settings.min_risk_reward,
        "max_position_size": settings.max_position_size,
        "max_symbol_exposure": settings.max_symbol_exposure,
        "max_total_exposure": settings.max_total_exposure,
        "technical_weight": settings.technical_weight,
        "quant_weight": settings.quant_weight,
        "strategy_weight": settings.strategy_weight,
        "regime_weight": settings.regime_weight,
        "news_weight": settings.news_weight,
        "sentiment_weight": settings.sentiment_weight,
    }