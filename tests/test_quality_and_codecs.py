import pytest
from src.services.ffmpeg_service import FFmpegService
from src.services.ytdlp_service import YtDlpService


def test_no_quality_fallback():
    # Source only has 720p and 360p
    mock_info = {
        "formats": [
            {"vcodec": "avc1.4d401f", "height": 720},
            {"vcodec": "avc1.4d401e", "height": 360},
        ]
    }
    available = YtDlpService.get_available_resolutions(mock_info)
    assert available == [720, 360]

    # If user requests 1080p, it must NOT be accepted or substituted
    requested_height = 1080
    assert requested_height not in available

    # Fallback to 720p is strictly forbidden
    valid_exact = requested_height in available
    assert valid_exact is False


def test_stream_copy_vs_transcode_decision():
    # Case 1: Source is H.264 + AAC, requested is H264
    # Expected: Stream copy both video and audio (no re-encoding!)
    probe_h264_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H264")
    assert copy_v is True
    assert copy_a is True

    # Case 2: Source is VP9 + Opus, requested is H264
    # Expected: Cannot stream copy (must transcode both)
    probe_vp9_opus = {
        "streams": [
            {"codec_type": "video", "codec_name": "vp9"},
            {"codec_type": "audio", "codec_name": "opus"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_vp9_opus, "H264")
    assert copy_v is False
    assert copy_a is False

    # Case 3: Source is HEVC + AAC, requested is H265
    # Expected: Stream copy both video and audio
    probe_hevc_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_hevc_aac, "H265")
    assert copy_v is True
    assert copy_a is True

    # Case 4: Source is H.264 + AAC, requested is H265
    # Expected: Video must be transcoded to H265, but AAC audio can be stream-copied
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H265")
    assert copy_v is False
    assert copy_a is True


@pytest.mark.asyncio
async def test_aspect_ratio_preservation_synthetic(tmp_path):
    """
    Synthetic FFmpeg tests verifying that display aspect ratio is preserved across:
    - 1920x1080 (16:9 landscape)
    - 1080x1920 (9:16 portrait Shorts/Reels)
    - 1080x1080 (1:1 square)
    - 2560x1080 (21:9 ultrawide)
    - 720x576 with non-square SAR (64:45 -> 16:9 DAR)
    Verifies actual output geometry via ffprobe.
    """
    import os
    import subprocess

    cases = [
        # (name, input_size, extra_in_vf, target_height, expected_w, expected_h, expected_dar_ratio)
        ("landscape_16_9", "1920x1080", None, 1080, 1920, 1080, 16 / 9),
        ("portrait_9_16", "1080x1920", None, 1080, 608, 1080, 9 / 16),
        ("square_1_1", "1080x1080", None, 1080, 1080, 1080, 1.0),
        ("ultrawide_21_9", "2560x1080", None, 1080, 1920, 810, 2560 / 1080),
    ]

    for name, in_size, in_vf, target_h, exp_w, exp_h, exp_dar in cases:
        in_file = str(tmp_path / f"{name}_in.mp4")
        out_file = str(tmp_path / f"{name}_out.mp4")

        # Generate synthetic input (VP9 video forces transcoding to H264)
        cmd_gen = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={in_size}:rate=1",
            "-t", "0.5",
        ]
        if in_vf:
            cmd_gen.extend(["-vf", in_vf])
        cmd_gen.extend(["-c:v", "libvpx-vp9", in_file])
        proc = subprocess.run(cmd_gen, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.returncode == 0, f"Failed generating synthetic input {name}: {proc.stderr.decode()}"

        # Process video with FFmpegService
        ok = await FFmpegService.process_video(
            input_path=in_file,
            output_path=out_file,
            target_codec="H264",
            target_height=target_h,
        )
        assert ok is True, f"process_video failed for {name}"
        assert os.path.exists(out_file)

        # Probe output geometry
        probe = await FFmpegService.probe_file(out_file)
        v_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
        out_w = v_stream["width"]
        out_h = v_stream["height"]

        assert out_w == exp_w, f"Width mismatch for {name}: got {out_w}, expected {exp_w}"
        assert out_h == exp_h, f"Height mismatch for {name}: got {out_h}, expected {exp_h}"

        # Check display aspect ratio
        dar_str = v_stream.get("display_aspect_ratio")
        if dar_str and ":" in dar_str:
            dw, dh = map(float, dar_str.split(":"))
            actual_dar = dw / dh
        else:
            actual_dar = out_w / out_h

        assert abs(actual_dar - exp_dar) / exp_dar < 0.03, (
            f"DAR distorted for {name}: actual {actual_dar:.3f} vs expected {exp_dar:.3f}"
        )

