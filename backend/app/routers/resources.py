from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MaintenanceWindow, Site, Vehicle
from ..schemas import MaintenanceOut, SiteOut, VehicleOut

router = APIRouter(prefix="/api", tags=["resources"])


@router.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(db: Session = Depends(get_db)):
    return db.scalars(select(Vehicle).order_by(Vehicle.id)).all()


@router.get("/sites", response_model=list[SiteOut])
def list_sites(db: Session = Depends(get_db)):
    return db.scalars(select(Site).order_by(Site.id)).all()


@router.get("/sites/{site_id}", response_model=SiteOut)
def get_site(site_id: int, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    return site


@router.get("/connectors/{connector_id}/maintenance", response_model=list[MaintenanceOut])
def connector_maintenance(connector_id: int, db: Session = Depends(get_db)):
    return db.scalars(
        select(MaintenanceWindow)
        .where(MaintenanceWindow.connector_id == connector_id)
        .order_by(MaintenanceWindow.start_at)
    ).all()


@router.get("/maintenance", response_model=list[MaintenanceOut])
def all_maintenance(db: Session = Depends(get_db)):
    return db.scalars(
        select(MaintenanceWindow).order_by(MaintenanceWindow.id)
    ).all()
