import asyncio
import pytest
from tests.mock_redis import MockRedis
from src.services.queue_service import QueueService


@pytest.fixture(autouse=True)
def patch_redis(monkeypatch):
    mock_r = MockRedis()
    monkeypatch.setattr("src.services.queue_service.get_redis_client", lambda: mock_r)
    return mock_r


@pytest.mark.asyncio
async def test_fifo_queue_ordering():
    # Push 3 jobs in order
    p1 = await QueueService.push_job("job-1")
    p2 = await QueueService.push_job("job-2")
    p3 = await QueueService.push_job("job-3")

    assert p1 == 1
    assert p2 == 2
    assert p3 == 3

    # Derived position check
    assert await QueueService.get_derived_position("job-1") == 1
    assert await QueueService.get_derived_position("job-2") == 2
    assert await QueueService.get_derived_position("job-3") == 3

    # Acquire: should pop job-1 first (FIFO)
    acquired = await QueueService.acquire_next_job(max_active_jobs=1)
    assert acquired == "job-1"

    # Now job-2 should have derived position #1
    assert await QueueService.get_derived_position("job-2") == 1
    assert await QueueService.get_derived_position("job-3") == 2


@pytest.mark.asyncio
async def test_concurrency_race_condition_limit_1():
    # MAX_ACTIVE_JOBS = 1
    await QueueService.push_job("job-A")
    await QueueService.push_job("job-B")

    # Simulate 3 concurrent workers attempting to acquire slot simultaneously
    results = await asyncio.gather(
        QueueService.acquire_next_job(max_active_jobs=1),
        QueueService.acquire_next_job(max_active_jobs=1),
        QueueService.acquire_next_job(max_active_jobs=1),
    )

    # Exactly ONE worker should acquire a job, other two must be None
    acquired_jobs = [r for r in results if r is not None]
    assert len(acquired_jobs) == 1
    assert acquired_jobs[0] == "job-A"

    # Active count is 1
    active = await QueueService.get_active_job_ids()
    assert len(active) == 1
    assert "job-A" in active

    # When job-A is released, job-B can be acquired
    await QueueService.release_active_job("job-A")
    next_job = await QueueService.acquire_next_job(max_active_jobs=1)
    assert next_job == "job-B"


@pytest.mark.asyncio
async def test_concurrency_limit_2():
    # MAX_ACTIVE_JOBS = 2
    await QueueService.push_job("job-1")
    await QueueService.push_job("job-2")
    await QueueService.push_job("job-3")

    results = await asyncio.gather(
        QueueService.acquire_next_job(max_active_jobs=2),
        QueueService.acquire_next_job(max_active_jobs=2),
        QueueService.acquire_next_job(max_active_jobs=2),
    )

    # Exactly TWO workers acquire jobs
    acquired_jobs = [r for r in results if r is not None]
    assert len(acquired_jobs) == 2
    assert "job-1" in acquired_jobs
    assert "job-2" in acquired_jobs
    assert results.count(None) == 1


@pytest.mark.asyncio
async def test_dynamic_concurrency_downscale():
    # 2 active jobs running under MAX_ACTIVE_JOBS=2
    await QueueService.push_job("active-1")
    await QueueService.push_job("active-2")
    await QueueService.push_job("queued-3")

    j1 = await QueueService.acquire_next_job(max_active_jobs=2)
    j2 = await QueueService.acquire_next_job(max_active_jobs=2)
    assert j1 == "active-1"
    assert j2 == "active-2"

    # Admin dynamically changes limit: 2 -> 1
    # Current jobs must NOT be terminated, but queued-3 cannot start yet!
    j3 = await QueueService.acquire_next_job(max_active_jobs=1)
    assert j3 is None  # Slot blocked because active_count (2) >= max_active (1)

    # Release active-1: active count is now 1, which still >= max_active (1)
    await QueueService.release_active_job("active-1")
    j3_attempt2 = await QueueService.acquire_next_job(max_active_jobs=1)
    assert j3_attempt2 is None

    # Release active-2: active count is now 0 (< 1), so queued-3 can now start!
    await QueueService.release_active_job("active-2")
    j3_attempt3 = await QueueService.acquire_next_job(max_active_jobs=1)
    assert j3_attempt3 == "queued-3"


@pytest.mark.asyncio
async def test_pause_and_resume_queue():
    await QueueService.push_job("job-paused-1")

    # Pause
    await QueueService.pause_queue()
    assert await QueueService.is_paused() is True

    # Cannot acquire while paused
    res = await QueueService.acquire_next_job(max_active_jobs=1)
    assert res is None

    # Resume
    await QueueService.resume_queue()
    assert await QueueService.is_paused() is False

    # Can acquire again
    res = await QueueService.acquire_next_job(max_active_jobs=1)
    assert res == "job-paused-1"
