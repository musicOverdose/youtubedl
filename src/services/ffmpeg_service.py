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
                    "-vf", f"scale=-2:{target_height}",
                ])
            else:  # H265
                cmd.extend([
                    "-c:v", "libx265",
                    "-preset", "ultrafast",  # Keep low CPU on 2 cores
                    "-crf", "28",
                    "-pix_fmt", "yuv420p",
                    "-vf", f"scale=-2:{target_height}",
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
