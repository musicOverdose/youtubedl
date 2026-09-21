import asyncio
import json
import os
import signal
from typing import Dict, List, Optional, Tuple
from src.core.logger import setup_logger

logger = setup_logger("ffmpeg_service")


class FFmpegService:
    @staticmethod
    async def probe_file(file_path: str) -> Dict:
        """Runs ffprobe on the media file to inspect audio/video streams."""
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"ffprobe failed for {file_path}: {stderr.decode()}")
            return {}
        try:
            return json.loads(stdout.decode())
        except Exception as e:
            logger.error(f"Error parsing ffprobe output: {e}")
            return {}

    @classmethod
    def can_stream_copy(
        cls,
        probe_data: Dict,
        target_codec: str,  # "H264" or "H265"
    ) -> Tuple[bool, bool]:
        """
        Determines if video and/or audio can be stream-copied (-c:v copy, -c:a copy).
        Returns: (can_copy_video, can_copy_audio)
        """
        streams = probe_data.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        can_copy_v = False
        can_copy_a = False

        if v_stream:
            vcodec = (v_stream.get("codec_name") or "").lower()
            if target_codec.upper() == "H264":
                # h264, avc1 can be stream-copied
                if vcodec in ("h264", "avc1"):
                    can_copy_v = True
            elif target_codec.upper() == "H265":
                # hevc, h265 can be stream-copied
                if vcodec in ("hevc", "h265"):
                    can_copy_v = True

        if a_stream:
            acodec = (a_stream.get("codec_name") or "").lower()
            # MP4 supports AAC without re-encoding
            if acodec == "aac":
                can_copy_a = True

        return can_copy_v, can_copy_a

    @classmethod
    def build_scale_filter(cls, target_height: int) -> str:
        """
        Builds DAR-preserving scaling filter chain.
        Uses 'dar' so display aspect ratio (including non-square sample aspect ratios) is preserved.
        scale='if(gt(dar,16/9),{max_w},-2)':'if(gt(dar,16/9),-2,{target_height})',pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1
        """
        max_w = int(round(target_height * (16 / 9)))
        if max_w % 2 != 0:
            max_w += 1
        return (
            f"scale='if(gt(dar,16/9),{max_w},-2)':'if(gt(dar,16/9),-2,{target_height})',"
            f"pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1"
        )

    @classmethod
    async def process_video(
        cls,
        input_path: str,
        output_path: str,
        target_codec: str,  # "H264" or "H265"
        target_height: int,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """
        Remuxes or transcodes video to exact output format (MP4 with target_codec + AAC).
        Minimizes CPU by stream-copying whenever possible.
        """
        probe_data = await cls.probe_file(input_path)
        can_copy_v, can_copy_a = cls.can_stream_copy(probe_data, target_codec)

        cmd = ["ffmpeg", "-y", "-i", input_path]
        scale_vf = cls.build_scale_filter(target_height)

        # Video codec options
        if can_copy_v:
            logger.info(f"Stream-copying video for {input_path} (no re-encoding needed!)")
            cmd.extend(["-c:v", "copy"])
        else:
            logger.info(f"Transcoding video to {target_codec} for {input_path}")
            if target_codec.upper() == "H264":
                cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-vf", scale_vf,
                ])
            else:  # H265
                cmd.extend([
                    "-c:v", "libx265",
                    "-preset", "ultrafast",  # Keep low CPU on 2 cores
                    "-crf", "28",
                    "-pix_fmt", "yuv420p",
                    "-vf", scale_vf,
                ])

        # Audio codec options
        if can_copy_a:
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        # Movflags for fast streaming start
        cmd.extend(["-movflags", "+faststart", output_path])

        return await cls.run_subprocess(cmd, cancellation_event)

    @classmethod
    async def extract_mp3(
        cls,
        input_audio_path: str,
        output_mp3_path: str,
        title: str,
        artist: Optional[str] = None,
        cover_image_path: Optional[str] = None,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Extracts and converts audio to MP3 with ID3 metadata and optional cover thumbnail."""
        cmd = ["ffmpeg", "-y", "-i", input_audio_path]

        has_cover = cover_image_path and os.path.exists(cover_image_path)
        if has_cover:
            cmd.extend(["-i", cover_image_path, "-map", "0:a", "-map", "1:0"])
        else:
            cmd.extend(["-map", "0:a"])

        cmd.extend([
            "-c:a", "libmp3lame",
            "-q:a", "2",  # VBR ~190 kbps high quality
            "-metadata", f"title={title}",
        ])

        if artist:
            cmd.extend(["-metadata", f"artist={artist}"])

        if has_cover:
            cmd.extend([
                "-c:v", "mjpeg",
                "-id3v2_version", "3",
                "-metadata:s:v", 'title="Album cover"',
                "-metadata:s:v", 'comment="Cover (front)"',
            ])

        cmd.append(output_mp3_path)
        return await cls.run_subprocess(cmd, cancellation_event)

    @staticmethod
    async def run_subprocess(
        cmd: List[str], cancellation_event: Optional[asyncio.Event] = None
    ) -> bool:
        """Runs ffmpeg command in a subprocess with cancellation and process group cleanup."""
        proc = None
        try:
            # Create process in a new process group so we can kill the whole tree
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,
            )

            async def wait_process():
                stdout, stderr = await proc.communicate()
                return proc.returncode, stderr

            process_task = asyncio.create_task(wait_process())

            if cancellation_event:
                cancel_task = asyncio.create_task(cancellation_event.wait())
                done, pending = await asyncio.wait(
                    [process_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_task in done:
                    # Cancelled! Kill entire process group
                    logger.warning(f"Process cancelled by user: killing PID group {proc.pid}")
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process_task.cancel()
                    return False

            returncode, stderr = await process_task
            if returncode != 0:
                logger.error(f"FFmpeg failed with code {returncode}: {stderr.decode()[-500:]}")
                return False
            return True

        except Exception as e:
            logger.error(f"FFmpeg subprocess error: {e}")
            if proc and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            return False

    @classmethod
    async def extract_video_metadata(cls, file_path: str) -> Tuple[int, int, int]:
        """
        Runs ffprobe against the FINAL processed output file and extracts:
        (duration, width, height).
        All must be valid positive values.
        Raises ValueError if ffprobe cannot determine valid positive metadata.
        """
        if not os.path.exists(file_path):
            raise ValueError(f"Video file does not exist: {file_path}")

        probe_data = await cls.probe_file(file_path)
        streams = probe_data.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not v_stream:
            raise ValueError(f"No video stream found in final output file: {file_path}")

        raw_width = v_stream.get("width")
        raw_height = v_stream.get("height")
        if raw_width is None or raw_height is None:
            raise ValueError(f"Video dimensions missing in ffprobe for {file_path}")

        try:
            width = int(raw_width)
            height = int(raw_height)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid video dimensions in {file_path}: width={raw_width}, height={raw_height}") from e

        if width <= 0 or height <= 0:
            raise ValueError(f"Video dimensions must be positive integers: width={width}, height={height}")

        # Duration can be in video stream duration, container format duration, or tags
        raw_duration = v_stream.get("duration")
        if raw_duration is None:
            raw_duration = probe_data.get("format", {}).get("duration")

        if raw_duration is None:
            raise ValueError(f"Video duration missing in ffprobe for {file_path}")

        try:
            duration_float = float(raw_duration)
            duration = int(round(duration_float))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid video duration in {file_path}: {raw_duration}") from e

        if duration <= 0:
            if duration_float > 0:
                duration = 1
            else:
                raise ValueError(f"Video duration must be positive: {duration_float}")

        return duration, width, height

    @classmethod
    async def generate_thumbnail(
        cls,
        video_path: str,
        output_thumb_path: str,
        duration: Optional[int] = None,
    ) -> bool:
        """
        Generates a JPEG thumbnail from the final processed video.
        Requirements:
        - JPEG format
        - Maximum 320x320
        - Strictly below 200 KB
        - Preserves display aspect ratio (using dar filter)
        - No stretching/squeezing
        - Chooses a sensible timestamp (avoiding frame 0 and black frames)
        - If first candidate is black and video is long enough, tries another timestamp
        - Resizes/recompresses as necessary to stay below the 200 KB limit
        """
        if not os.path.exists(video_path):
            logger.warning(f"Cannot generate thumbnail, video path missing: {video_path}")
            return False

        dur = duration
        if dur is None:
            try:
                dur, _, _ = await cls.extract_video_metadata(video_path)
            except Exception:
                dur = 1

        initial_seek = min(1.0, float(dur) * 0.1) if dur > 2 else 0.0

        scale_vf = "scale='if(gte(dar,1),320,-2)':'if(gte(dar,1),-2,320)',pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1"

        async def _extract_frame(seek_t: float) -> bool:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seek_t),
                "-i", video_path,
                "-vframes", "1",
                "-vf", scale_vf,
                "-q:v", "2",
                output_thumb_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.debug("Thumbnail ffmpeg extraction at %ss failed: %s", seek_t, stderr.decode()[-300:])
                return False
            return os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 0

        ok = await _extract_frame(initial_seek)
        if not ok and initial_seek > 0.0:
            ok = await _extract_frame(0.0)

        if not ok:
            logger.warning("FFmpeg failed to extract thumbnail frame from %s", video_path)
            return False

        try:
            from PIL import Image

            def _inspect_and_optimize():
                with Image.open(output_thumb_path) as img:
                    img = img.convert("RGB")
                    is_black = False
                    grayscale = img.convert("L")
                    extrema = grayscale.getextrema()
                    if extrema and extrema[1] < 15:
                        is_black = True
                    return is_black, img.size

            loop = asyncio.get_running_loop()
            is_black, (w, h) = await loop.run_in_executor(None, _inspect_and_optimize)

            if is_black and dur > 4:
                alt_seek = min(3.0, float(dur) * 0.25)
                logger.info("First thumbnail candidate at %ss is black/dark; trying %ss", initial_seek, alt_seek)
                alt_ok = await _extract_frame(alt_seek)
                if not alt_ok:
                    await _extract_frame(initial_seek)

            def _ensure_limits():
                with Image.open(output_thumb_path) as img:
                    img = img.convert("RGB")
                    w, h = img.size
                    modified = False
                    if w > 320 or h > 320:
                        img.thumbnail((320, 320), Image.Resampling.LANCZOS)
                        modified = True

                    max_bytes = 195 * 1024
                    file_size = os.path.getsize(output_thumb_path)

                    if modified or file_size > max_bytes:
                        quality = 85
                        img.save(output_thumb_path, "JPEG", quality=quality, optimize=True)
                        file_size = os.path.getsize(output_thumb_path)
                        while file_size > max_bytes and quality > 30:
                            quality -= 15
                            img.save(output_thumb_path, "JPEG", quality=quality, optimize=True)
                            file_size = os.path.getsize(output_thumb_path)

            await loop.run_in_executor(None, _ensure_limits)
            return True

        except Exception as e:
            logger.warning("Error inspecting/optimizing thumbnail: %s", e)
            if os.path.exists(output_thumb_path) and 0 < os.path.getsize(output_thumb_path) < 200 * 1024:
                return True
            return False

