import asyncio
import glob
import os
import shutil
import tempfile
from typing import Optional, Tuple, Dict, Any, List
from PIL import Image
import httpx

from src.core.logger import get_logger

logger = get_logger("thumbnail_service")


class ThumbnailService:
    """
    Dedicated high-fidelity thumbnail processing service.
    
    Guarantees:
    1. Highest-quality official YouTube creator thumbnail is always prioritized.
    2. Zero video re-encoding (thumbnail pipeline is decoupled from video stream).
    3. Strict aspect ratio preservation (no stretching, squishing, or distortion).
    4. High-quality single-pass Lanczos downsampling (Image.Resampling.LANCZOS).
    5. Adaptive JPEG compression starting at quality=95 with 4:4:4 chroma subsampling (subsampling=0).
    6. Telegram Bot API compliance: JPEG format, width <= 320, height <= 320, file size < 200 KB.
    7. High-resolution FFmpeg frame extraction fallback only when no official thumbnail exists.
    """

    MAX_DIMENSION: int = 320
    MAX_FILE_BYTES: int = 195 * 1024  # 195 KB (strict Telegram limit is 200 KB)

    @classmethod
    def get_best_thumbnail_url(cls, info: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Inspects yt-dlp metadata dictionary and selects the highest-quality
        available official YouTube thumbnail URL based on width, height,
        preference, and resolution tier heuristics.
        """
        if not info:
            return None

        thumbnails = info.get("thumbnails")
        if not thumbnails or not isinstance(thumbnails, list):
            return info.get("thumbnail")

        valid_thumbs = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
        if not valid_thumbs:
            return info.get("thumbnail")

        def _thumb_score(t: Dict[str, Any]) -> Tuple[int, int, int]:
            url = str(t.get("url") or "")
            pref = t.get("preference") if t.get("preference") is not None else 0
            w = t.get("width") or 0
            h = t.get("height") or 0
            area = w * h

            url_lower = url.lower()
            id_lower = str(t.get("id") or "").lower()

            name_bonus = 0
            if "maxres" in url_lower or "maxres" in id_lower:
                name_bonus = 1920 * 1080
            elif "hq720" in url_lower or "hq720" in id_lower:
                name_bonus = 1280 * 720
            elif "sddefault" in url_lower or "sddefault" in id_lower:
                name_bonus = 640 * 480
            elif "hqdefault" in url_lower or "hqdefault" in id_lower:
                name_bonus = 480 * 360
            elif "mqdefault" in url_lower or "mqdefault" in id_lower:
                name_bonus = 320 * 180

            effective_area = max(area, name_bonus)
            is_jpeg = 1 if (".jpg" in url_lower or ".jpeg" in url_lower) else 0

            return (pref, effective_area, is_jpeg)

        best = max(valid_thumbs, key=_thumb_score)
        return best.get("url") or info.get("thumbnail")

    @classmethod
    def fit_dimensions_inside_bounds(
        cls,
        w: int,
        h: int,
        max_w: int = MAX_DIMENSION,
        max_h: int = MAX_DIMENSION,
    ) -> Tuple[int, int]:
        """
        Calculates output dimensions to fit strictly inside (max_w, max_h)
        while preserving the EXACT original display aspect ratio.
        Never stretches, squeezes, or distorts geometry.
        """
        if w <= 0 or h <= 0:
            return (max_w, max_h)

        scale = min(max_w / float(w), max_h / float(h))
        new_w = max(1, min(max_w, int(round(w * scale))))
        new_h = max(1, min(max_h, int(round(h * scale))))
        return (new_w, new_h)

    @classmethod
    def save_image_adaptive_jpeg(
        cls,
        img: Image.Image,
        output_path: str,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> bool:
        """
        Saves a PIL Image to output_path as a high-fidelity JPEG strictly <= max_bytes.
        Starts with quality=95 and 4:4:4 chroma subsampling (subsampling=0) for sharp text/edges.
        Dynamically adapts quality and subsampling if necessary to ensure compliance.
        """
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Pass 1: 4:4:4 chroma subsampling (subsampling=0) - zero color smearing
        for q in [95, 92, 90, 88, 85, 80]:
            img.save(output_path, "JPEG", quality=q, optimize=True, subsampling=0)
            if os.path.exists(output_path) and os.path.getsize(output_path) <= max_bytes:
                return True

        # Pass 2: Standard 4:2:0 subsampling if initial pass exceeded max_bytes
        for q in [85, 80, 75, 70, 60, 50]:
            img.save(output_path, "JPEG", quality=q, optimize=True)
            if os.path.exists(output_path) and os.path.getsize(output_path) <= max_bytes:
                return True

        return False

    @classmethod
    def process_image_file(
        cls,
        source_image_path: str,
        output_thumb_path: str,
    ) -> bool:
        """
        Opens source_image_path (JPEG, WebP, PNG), scales it with Lanczos resampling
        to fit inside MAX_DIMENSION x MAX_DIMENSION preserving aspect ratio,
        and saves as a compliant JPEG.
        """
        if not os.path.exists(source_image_path) or os.path.getsize(source_image_path) == 0:
            return False

        try:
            with Image.open(source_image_path) as img:
                img = img.convert("RGB")
                src_w, src_h = img.size
                target_w, target_h = cls.fit_dimensions_inside_bounds(
                    src_w, src_h, cls.MAX_DIMENSION, cls.MAX_DIMENSION
                )

                if (src_w, src_h) != (target_w, target_h):
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

                ok = cls.save_image_adaptive_jpeg(img, output_thumb_path, cls.MAX_FILE_BYTES)
                if ok and os.path.exists(output_thumb_path):
                    final_size = os.path.getsize(output_thumb_path)
                    logger.info(
                        "Processed thumbnail from %s: size=(%d, %d), file_size=%d bytes",
                        source_image_path, target_w, target_h, final_size
                    )
                    return True
                return False
        except Exception as e:
            logger.warning("Error processing thumbnail image %s: %s", source_image_path, e)
            return False

    @classmethod
    async def download_thumbnail_image(
        cls,
        url: str,
        output_path: str,
        timeout: float = 15.0,
    ) -> bool:
        """
        Downloads a remote thumbnail image via HTTPX.
        """
        if not url:
            return False

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    return True
                logger.warning(
                    "Failed to download thumbnail from %s: status %d", url, resp.status_code
                )
                return False
        except Exception as e:
            logger.warning("HTTP error downloading thumbnail from %s: %s", url, e)
            return False

    @classmethod
    async def extract_frame_fallback(
        cls,
        video_path: str,
        output_thumb_path: str,
        duration: Optional[int] = None,
    ) -> bool:
        """
        Fallback when no official YouTube thumbnail exists:
        Extracts a representative native-resolution video frame using FFmpeg,
        checks for dark/black frames, and processes via Pillow Lanczos.
        """
        if not os.path.exists(video_path):
            logger.warning("Video file does not exist for fallback extraction: %s", video_path)
            return False

        dur = duration
        if dur is None or dur <= 0:
            dur = 10

        # Primary timestamp at ~15% of duration (avoids 0.0s/1.0s intros and black frames)
        primary_seek = min(15.0, float(dur) * 0.15) if dur > 2 else 0.0
        candidate_seeks = [primary_seek]
        if dur > 4:
            candidate_seeks.append(min(30.0, float(dur) * 0.25))
            candidate_seeks.append(float(dur) * 0.50)
        if 0.0 not in candidate_seeks:
            candidate_seeks.append(0.0)

        temp_dir = tempfile.mkdtemp(prefix="thumb_extract_")
        try:
            best_frame_path = None
            for seek_t in candidate_seeks:
                frame_path = os.path.join(temp_dir, f"frame_{seek_t:.2f}.png")
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{seek_t:.3f}",
                    "-i", video_path,
                    "-vframes", "1",
                    "-q:v", "1",
                    frame_path,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

                if proc.returncode == 0 and os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                    try:
                        with Image.open(frame_path) as img:
                            grayscale = img.convert("L")
                            extrema = grayscale.getextrema()
                            # Check if frame is not solid black (max luminance >= 20)
                            if extrema and extrema[1] >= 20:
                                best_frame_path = frame_path
                                logger.info(
                                    "Extracted non-black frame at %.2fs for fallback thumbnail", seek_t
                                )
                                break
                            else:
                                logger.debug("Extracted frame at %.2fs is black/dark; trying next candidate", seek_t)
                                if best_frame_path is None:
                                    best_frame_path = frame_path
                    except Exception as img_err:
                        logger.debug("Error inspecting extracted frame: %s", img_err)

            if not best_frame_path or not os.path.exists(best_frame_path):
                logger.warning("FFmpeg fallback frame extraction failed for %s", video_path)
                return False

            # Process the extracted native frame through Pillow Lanczos pipeline
            return cls.process_image_file(best_frame_path, output_thumb_path)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @classmethod
    async def prepare_video_thumbnail(
        cls,
        video_path: str,
        output_thumb_path: str,
        source_thumb_path: Optional[str] = None,
        source_thumb_url: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> bool:
        """
        Orchestrates thumbnail preparation with strict priority:
        1. Local official YouTube thumbnail file (from yt-dlp writethumbnail).
        2. Remote official YouTube thumbnail URL (highest quality from metadata).
        3. Fallback native-resolution video frame extraction via FFmpeg.
        """
        # Priority 1: Check existing local official thumbnail file
        if source_thumb_path and os.path.exists(source_thumb_path) and os.path.getsize(source_thumb_path) > 0:
            logger.info("Preparing thumbnail using local official YouTube artwork: %s", source_thumb_path)
            ok = cls.process_image_file(source_thumb_path, output_thumb_path)
            if ok:
                return True
            logger.warning("Failed processing local official thumbnail; checking fallback URL")

        # Priority 2: Check remote official thumbnail URL
        if source_thumb_url:
            temp_thumb = output_thumb_path + ".download.tmp"
            try:
                logger.info("Downloading official YouTube thumbnail from: %s", source_thumb_url)
                dl_ok = await cls.download_thumbnail_image(source_thumb_url, temp_thumb)
                if dl_ok:
                    ok = cls.process_image_file(temp_thumb, output_thumb_path)
                    if ok:
                        return True
            finally:
                if os.path.exists(temp_thumb):
                    try:
                        os.remove(temp_thumb)
                    except OSError:
                        pass

        # Priority 3: Fallback native-resolution frame extraction via FFmpeg
        logger.info("No usable official YouTube thumbnail found. Falling back to FFmpeg frame extraction.")
        return await cls.extract_frame_fallback(video_path, output_thumb_path, duration)
