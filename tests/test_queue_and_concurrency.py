import asyncio
import pytest
from tests.mock_redis import MockRedis
from src.services.queue_service import QueueService


@pytest.fixture(autouse=True)
def patch_redis(monkeypatch):
    mock_r = MockRedis()
    monkeypatch.setattr("src.core.redis.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.queue_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.ytdlp_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.worker.recovery.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.worker.processor.get_redis_client", lambda: mock_r)
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


@pytest.mark.asyncio
async def test_video_and_subtitle_queue_routing():
    # 1. Video job
    pos_v1 = await QueueService.push_job("video-job-1", queue_type="VIDEO")
    assert pos_v1 == 1

    # 2. Audio job -> routes to VIDEO/media queue
    pos_v2 = await QueueService.push_job("audio-job-2", queue_type="VIDEO")
    assert pos_v2 == 2

    # 3. Subtitle job -> routes to SUBTITLE queue
    pos_s1 = await QueueService.push_job("sub-job-1", queue_type="SUBTITLE")
    assert pos_s1 == 1  # Independent position!

    # Derived positions
    assert await QueueService.get_derived_position("video-job-1", queue_type="VIDEO") == 1
    assert await QueueService.get_derived_position("audio-job-2", queue_type="VIDEO") == 2
    assert await QueueService.get_derived_position("sub-job-1", queue_type="SUBTITLE") == 1

    # Queued IDs
    assert await QueueService.get_queued_job_ids("VIDEO") == ["video-job-1", "audio-job-2"]
    assert await QueueService.get_queued_job_ids("SUBTITLE") == ["sub-job-1"]


@pytest.mark.asyncio
async def test_subtitle_jobs_do_not_block_video_jobs():
    # 2 Subtitle jobs fill subtitle concurrency slots (limit = 2)
    await QueueService.push_job("sub-1", queue_type="SUBTITLE")
    await QueueService.push_job("sub-2", queue_type="SUBTITLE")

    s1 = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    s2 = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    assert s1 == "sub-1"
    assert s2 == "sub-2"

    # Subtitle queue is now saturated (2 active)
    assert len(await QueueService.get_active_job_ids("SUBTITLE")) == 2

    # A 3rd subtitle job cannot start
    await QueueService.push_job("sub-3", queue_type="SUBTITLE")
    s3 = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    assert s3 is None

    # AT THE SAME TIME: A video job is submitted
    await QueueService.push_job("video-1", queue_type="VIDEO")

    # Video worker MUST acquire video-1 immediately without waiting!
    v1 = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    assert v1 == "video-1"
    assert len(await QueueService.get_active_job_ids("VIDEO")) == 1


@pytest.mark.asyncio
async def test_video_job_does_not_block_subtitle_jobs():
    # 1 Video job fills video concurrency slot (limit = 1)
    await QueueService.push_job("video-active-1", queue_type="VIDEO")
    v1 = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    assert v1 == "video-active-1"
    assert len(await QueueService.get_active_job_ids("VIDEO")) == 1

    # Video queue saturated: a 2nd video cannot start
    await QueueService.push_job("video-queued-2", queue_type="VIDEO")
    v2 = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    assert v2 is None

    # Subtitle jobs arrive
    await QueueService.push_job("sub-1", queue_type="SUBTITLE")
    await QueueService.push_job("sub-2", queue_type="SUBTITLE")

    # Subtitle worker MUST acquire both without being blocked by video!
    s1 = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    s2 = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    assert s1 == "sub-1"
    assert s2 == "sub-2"


@pytest.mark.asyncio
async def test_backlog_fairness_many_subtitles_do_not_starve_video():
    # 10 subtitle jobs pushed
    for i in range(1, 11):
        await QueueService.push_job(f"sub-{i}", queue_type="SUBTITLE")

    # 1 video job pushed
    await QueueService.push_job("urgent-video", queue_type="VIDEO")

    # Video job position is #1 in its queue, NOT #11!
    pos = await QueueService.get_derived_position("urgent-video", queue_type="VIDEO")
    assert pos == 1

    # Video worker acquires urgent-video immediately
    acquired_video = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    assert acquired_video == "urgent-video"


@pytest.mark.asyncio
async def test_failure_isolation_between_queues():
    # Video and subtitle both running
    await QueueService.push_job("video-job", queue_type="VIDEO")
    await QueueService.push_job("sub-job", queue_type="SUBTITLE")

    v = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    s = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    assert v == "video-job"
    assert s == "sub-job"

    # Subtitle job fails and is released
    await QueueService.release_active_job("sub-job", queue_type="SUBTITLE")
    assert len(await QueueService.get_active_job_ids("SUBTITLE")) == 0

    # Video active job is untouched and still running
    active_video = await QueueService.get_active_job_ids("VIDEO")
    assert active_video == ["video-job"]

    # Releasing video job cleans video active set
    await QueueService.release_active_job("video-job", queue_type="VIDEO")
    assert len(await QueueService.get_active_job_ids("VIDEO")) == 0


@pytest.mark.asyncio
async def test_startup_recovery_requeues_interrupted_jobs(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from src.models.job import Job
    from src.core.constants import JobStatus, OperationType, REDIS_KEY_ACTIVE_VIDEO, REDIS_KEY_ACTIVE_SUBTITLE
    from src.worker.recovery import WorkerRecovery
    from src.core.redis import get_redis_client

    # Create mock jobs in PostgreSQL
    job_v = Job(
        id="interrupted-video",
        source_url="https://youtube.com/watch?v=123",
        canonical_url="https://youtube.com/watch?v=123",
        source_id="123",
        title="Test Video",
        operation=OperationType.VIDEO.value,
        status=JobStatus.DOWNLOADING.value,
        cache_key="ck1",
    )
    job_s = Job(
        id="interrupted-sub",
        source_url="https://youtube.com/watch?v=456",
        canonical_url="https://youtube.com/watch?v=456",
        source_id="456",
        title="Test Sub",
        operation=OperationType.SUBTITLE.value,
        status=JobStatus.PROCESSING.value,
        cache_key="ck2",
    )

    r = get_redis_client()
    await r.sadd(REDIS_KEY_ACTIVE_VIDEO, "interrupted-video")
    await r.sadd(REDIS_KEY_ACTIVE_SUBTITLE, "interrupted-sub")

    # Mock session
    mock_session = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [job_v, job_s]
    mock_res = MagicMock()
    mock_res.scalars.return_value = mock_scalars

    mock_res_queued = MagicMock()
    mock_scalars_queued = MagicMock()
    mock_scalars_queued.all.return_value = []
    mock_res_queued.scalars.return_value = mock_scalars_queued

    mock_session.execute.side_effect = [mock_res, mock_res_queued]

    await WorkerRecovery.perform_startup_recovery(mock_session)

    # 1. Dangling active sets cleared
    assert len(await r.smembers(REDIS_KEY_ACTIVE_VIDEO)) == 0
    assert len(await r.smembers(REDIS_KEY_ACTIVE_SUBTITLE)) == 0

    # 2. Both jobs recovered to QUEUED
    assert job_v.status == JobStatus.QUEUED.value
    assert job_s.status == JobStatus.QUEUED.value

    # 3. Both jobs re-enqueued to their respective queues
    queued_video = await QueueService.get_queued_job_ids("VIDEO")
    queued_sub = await QueueService.get_queued_job_ids("SUBTITLE")
    assert "interrupted-video" in queued_video
    assert "interrupted-sub" in queued_sub

    # 4. Jobs can be acquired by workers again
    acq_v = await QueueService.acquire_next_job("VIDEO", max_active_jobs=1)
    acq_s = await QueueService.acquire_next_job("SUBTITLE", max_active_jobs=2)
    assert acq_v == "interrupted-video"
    assert acq_s == "interrupted-sub"


@pytest.mark.asyncio
async def test_simultaneous_video_and_subtitle_async_progress():
    events = []

    async def simulate_video_job():
        events.append("video_start")
        # Video download running via non-blocking async sleep (simulating run_in_executor/subprocess)
        await asyncio.sleep(0.05)
        events.append("video_remux")
        await asyncio.sleep(0.05)
        events.append("video_done")

    async def simulate_subtitle_job():
        events.append("sub_start")
        # Subtitle chunked AI translation
        for i in range(3):
            await asyncio.sleep(0.03)
            events.append(f"sub_chunk_{i+1}")
        events.append("sub_done")

    # Run both simultaneously
    await asyncio.gather(simulate_video_job(), simulate_subtitle_job())

    # Verify interleaved execution: neither blocked the event loop
    assert "video_start" in events
    assert "sub_start" in events
    assert "video_done" in events
    assert "sub_done" in events
    assert events.index("sub_chunk_1") < events.index("video_done")


@pytest.mark.asyncio
async def test_redis_metadata_l2_cache(monkeypatch):
    from src.services.ytdlp_service import YtDlpService
    from src.core.redis import get_redis_client
    import json

    r = get_redis_client()
    source_id = "dQw4w9WgXcQ"
    url = f"https://www.youtube.com/watch?v={source_id}"

    call_count = 0

    def mock_extract(cls, url, cookies_file=None):
        nonlocal call_count
        call_count += 1
        return {"id": source_id, "title": "Cached Title", "duration": 120, "formats": [{"vcodec": "avc1", "height": 720}]}

    # Clear memory cache for this ID
    from src.services import ytdlp_service
    ytdlp_service._metadata_cache.pop(source_id, None)

    # First call: misses caches, extracts via yt-dlp, populates L1 and L2
    monkeypatch.setattr(YtDlpService, "get_base_opts", lambda *a: {})
    import yt_dlp
    class MockYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            nonlocal call_count
            call_count += 1
            return {"id": source_id, "title": "Cached Title", "duration": 120, "formats": [{"vcodec": "avc1", "height": 720}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", MockYDL)

    info1 = await YtDlpService.extract_metadata(url)
    assert info1["title"] == "Cached Title"
    assert call_count == 1

    # Verify stored in Redis L2
    redis_key = f"ytdl:metadata:cache:{source_id}"
    cached_redis = await r.get(redis_key)
    assert cached_redis is not None

    # Clear L1 memory cache only (simulating different container or process)
    ytdlp_service._metadata_cache.pop(source_id, None)

    # Second call: hits L2 Redis cache without calling yt-dlp!
    info2 = await YtDlpService.extract_metadata(url)
    assert info2["title"] == "Cached Title"
    assert call_count == 1  # Not incremented!


@pytest.mark.asyncio
async def test_redis_metadata_payload_pruning_on_large_metadata(monkeypatch):
    from src.services.ytdlp_service import YtDlpService
    from src.core.redis import get_redis_client
    from src.services import ytdlp_service
    import json

    r = get_redis_client()
    source_id = "dQw4w9WgXcA"
    url = f"https://www.youtube.com/watch?v={source_id}"

    # Generate huge metadata (over 512 KB) with unnecessary verbose headers/fragments
    huge_formats = []
    for i in range(500):
        huge_formats.append({
            "format_id": f"{i}",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "width": 1920,
            "ext": "mp4",
            "http_headers": {"User-Agent": "A" * 1000, "Cookie": "B" * 1000},  # Heavy bloat
            "fragments": [{"url": f"http://frag/{j}", "duration": 2.0} for j in range(20)],
        })

    huge_info = {
        "id": source_id,
        "title": "Huge Video",
        "duration": 300,
        "formats": huge_formats,
    }

    raw_size = len(json.dumps(huge_info))
    assert raw_size > 512 * 1024  # Ensure it exceeds 512 KB

    ytdlp_service._metadata_cache.pop(source_id, None)

    monkeypatch.setattr(YtDlpService, "get_base_opts", lambda *a: {})
    import yt_dlp
    class MockYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, url, download=False):
            return huge_info

    monkeypatch.setattr(yt_dlp, "YoutubeDL", MockYDL)

    # Extract metadata: should prune heavy fields and store safe payload in Redis
    info = await YtDlpService.extract_metadata(url)
    assert info["title"] == "Huge Video"

    redis_key = f"ytdl:metadata:cache:{source_id}"
    cached_redis = await r.get(redis_key)
    assert cached_redis is not None
    assert len(cached_redis) <= 512 * 1024  # Safely pruned below 512 KB!

    cached_data = json.loads(cached_redis)
    # Essential format fields preserved
    assert cached_data["formats"][0]["format_id"] == "0"
    assert cached_data["formats"][0]["height"] == 1080
    assert cached_data["formats"][0]["vcodec"] == "avc1.640028"
    # Bloat removed
    assert "http_headers" not in cached_data["formats"][0]
    assert "fragments" not in cached_data["formats"][0]


@pytest.mark.asyncio
async def test_redis_english_subtitle_caching():
    from src.core.redis import get_redis_client
    from src.core.constants import REDIS_KEY_SUBTITLE_CACHE_PREFIX

    r = get_redis_client()
    source_id = "sub_cache_test"
    sub_key = f"{REDIS_KEY_SUBTITLE_CACHE_PREFIX}{source_id}"

    sample_srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nHello World\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\nTesting Subtitle Cache"
    )

    # Store in Redis
    await r.set(sub_key, sample_srt, ex=86400)

    # Verify retrieval
    cached = await r.get(sub_key)
    assert cached == sample_srt



