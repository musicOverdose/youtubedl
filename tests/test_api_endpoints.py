import pytest
from httpx import ASGITransport, AsyncClient
from src.web.app import app


@pytest.mark.asyncio
async def test_health_and_metrics_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"

        # 2. Metrics
        m_resp = await client.get("/metrics")
        assert m_resp.status_code == 200
        text = m_resp.text
        assert "ytdl_jobs_completed_total" in text
        assert "ytdl_queue_active_jobs" in text

        # 3. Unauthorized access to protected route
        prot_resp = await client.get("/api/dashboard/stats")
        assert prot_resp.status_code == 401
