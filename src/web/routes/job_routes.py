from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_db
from src.models.job import Job
from src.models.job_request import JobRequest
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


@router.get("")
async def list_jobs(
    status: Optional[str] = None,
    operation: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    query = select(Job)
    count_query = select(func.count(Job.id))

    if status:
        query = query.where(Job.status == status)
        count_query = count_query.where(Job.status == status)
    if operation:
        query = query.where(Job.operation == operation)
        count_query = count_query.where(Job.operation == operation)
    if search:
        search_filter = Job.title.ilike(f"%{search}%") | Job.source_id.ilike(f"%{search}%")
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    total = await session.scalar(count_query) or 0
    query = query.order_by(desc(Job.created_at)).offset(offset).limit(limit)
    res = await session.execute(query)
    jobs = res.scalars().all()

    items = []
    for j in jobs:
        items.append({
            "id": j.id,
            "source_id": j.source_id,
            "title": j.title,
            "operation": j.operation,
            "codec": j.output_codec,
            "resolution": j.resolution,
            "status": j.status,
            "cache_key": j.cache_key,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            "error_code": j.error_code,
            "error_message": j.error_message,
        })

    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/{job_id}")
async def get_job_detail(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(Job).where(Job.id == job_id)
    res = await session.execute(stmt)
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    req_stmt = select(JobRequest).where(JobRequest.job_id == job_id)
    req_res = await session.execute(req_stmt)
    requests = req_res.scalars().all()

    return {
        "job": {
            "id": job.id,
            "source_id": job.source_id,
            "canonical_url": job.canonical_url,
            "title": job.title,
            "operation": job.operation,
            "codec": job.output_codec,
            "resolution": job.resolution,
            "status": job.status,
            "cache_key": job.cache_key,
            "progress": job.progress,
            "speed": job.speed,
            "eta": job.eta,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "error_code": job.error_code,
            "error_message": job.error_message,
        },
        "requests": [
            {
                "user_id": r.user_id,
                "chat_id": r.chat_id,
                "delivery_status": r.delivery_status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
            }
            for r in requests
        ],
    }
