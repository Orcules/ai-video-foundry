"""Sheet-mode helper methods extracted from VideoSceneProcessor.
These methods are used only in Google Sheets standalone mode (not API mode).
"""
import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.services.ffmpeg_processor import FFmpegProcessor
from tvd_pipeline.services.local_ffmpeg import LocalFFmpegFallback
from tvd_pipeline.services.tasks.video_analysis import _generate_image_prompt
from tvd_pipeline.services.tasks.prompt_parsing import generate_influencer_prompts
from tvd_pipeline.services.tasks.voiceover import generate_influencer_vo_script
from tvd_pipeline.services.tasks.music import generate_music_description_from_text
from tvd_pipeline.services.tasks.subtitle_enrichment import enrich_transcript_for_subtitles
from tvd_pipeline.config import get_pipeline_defaults

config = Config()
logger = logging.getLogger(__name__)


def run_rendi_zapcap_only(
    processor,
    row_num: int,
    headers: List[str],
    scene_videos: List[str],
    scene_durations: List[float],
    music_url: Optional[str] = None,
    vo_audio_urls: Optional[List[str]] = None,
    vo_audio_url: Optional[str] = None,
    add_subtitles: bool = False,
    subtitle_language: str = "en",
    buffer_seconds: float = 0.5
) -> Dict[str, Any]:
    """Run only Rendi (concat + audio) and ZapCap for existing assets. No generation.

    Use when row already has Scene N - new video, New music, New Voice / VO filled.
    """
    result = {"row": row_num, "success": False, "errors": [], "final_video_url": None}
    if not scene_videos:
        result["errors"].append("No scene videos provided")
        return result
    n = len(scene_videos)
    if len(scene_durations) != n:
        scene_durations = [scene_durations[0] if scene_durations else 3.0] * n
    has_per_scene_vo = bool(vo_audio_urls) and len(vo_audio_urls) >= n and any(vo_audio_urls[:n])
    has_single_vo = bool(vo_audio_url) and not has_per_scene_vo
    has_music = bool(music_url)
    buffer_sec = buffer_seconds if buffer_seconds > 0 else 0.0

    # When we have per-scene VO, set each scene duration to VO length + 0.5s (so VO plays in full)
    if has_per_scene_vo and vo_audio_urls:
        vo_extra_sec = 0.5
        for i in range(n):
            if i >= len(vo_audio_urls) or not vo_audio_urls[i]:
                continue
            vo_dur = processor.rendi_service.get_audio_duration_cloud(vo_audio_urls[i])
            if vo_dur <= 0 and FFmpegProcessor.check_ffmpeg_installed():
                vo_dur = FFmpegProcessor.get_audio_duration(vo_audio_urls[i])
            if vo_dur > 0:
                scene_durations[i] = max(vo_dur + vo_extra_sec, 1.0)
                if i < n - 1 and buffer_sec > 0:
                    scene_durations[i] += buffer_sec
                logger.info(f"   Scene {i + 1}: duration from VO = {scene_durations[i]:.2f}s (VO {vo_dur:.2f}s + {vo_extra_sec}s)")

    video_urls_for_concat = []
    if has_per_scene_vo:
        for i, video_url in enumerate(scene_videos):
            if not video_url:
                continue
            scene_vo_url = vo_audio_urls[i] if i < len(vo_audio_urls) else None
            if scene_vo_url:
                scene_with_vo = processor.rendi_service.add_audio_to_video(video_url=video_url, audio_url=scene_vo_url)
                if not scene_with_vo and FFmpegProcessor.check_ffmpeg_installed():
                    scene_with_vo = LocalFFmpegFallback.add_audio_to_video(
                        processor.gcs_storage_service, video_url, scene_vo_url
                    )
                video_urls_for_concat.append(scene_with_vo or video_url)
            else:
                video_urls_for_concat.append(video_url)
    else:
        video_urls_for_concat = [v for v in scene_videos if v]

    video_data = []
    for i, video_url in enumerate(video_urls_for_concat):
        dur = scene_durations[i] if i < len(scene_durations) else 3.0
        # When we didn't set durations from VO (no per-scene VO), add buffer for non-last scenes
        if not has_per_scene_vo and buffer_sec > 0 and i < len(video_urls_for_concat) - 1:
            dur += buffer_sec
        video_data.append({"video_url": video_url, "duration": dur})

    # When per-scene VO: always trim so each clip = VO length + 0.5s (durations already set above)
    if has_per_scene_vo and video_data:
        video_data = processor.rendi_service.trim_videos_batch(
            video_data, add_buffer_except_last=False, videos_have_audio=True
        )
        if not video_data and FFmpegProcessor.check_ffmpeg_installed():
            video_data = []
            for i, item in enumerate(video_urls_for_concat):
                dur = scene_durations[i] if i < len(scene_durations) else 3.0
                u = LocalFFmpegFallback.trim_video(processor.gcs_storage_service, item, dur)
                video_data.append({"video_url": u or item, "duration": dur})
        if not video_data:
            video_data = [{"video_url": u, "duration": scene_durations[i] if i < len(scene_durations) else 3.0} for i, u in enumerate(video_urls_for_concat)]

    concat_video_url = processor.rendi_service.concatenate_videos(
        video_data=video_data, assume_clips_have_audio=has_per_scene_vo
    )
    concat_has_audio = bool(concat_video_url)  # Rendi concat with audio
    if not concat_video_url and FFmpegProcessor.check_ffmpeg_installed():
        concat_video_url = LocalFFmpegFallback.concat_video_only(processor.gcs_storage_service, video_data)
        concat_has_audio = False  # Local fallback is video-only
    if not concat_video_url:
        result["errors"].append("Concatenation failed")
        return result

    try:
        processor.sheets_service.update_cell(
            config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
            config.RENDI_SCENE_COLUMN, concat_video_url, headers
        )
    except Exception:
        pass

    rendi_scene_voice_url = concat_video_url
    if has_per_scene_vo and has_music:
        # Only assume audio when concat output has VO (Rendi concat); local fallback is video-only
        assume_audio = concat_has_audio
        rendi_scene_voice_url = processor.rendi_service.add_background_music_to_video(
            concat_video_url, music_url, 0.2, assume_has_audio=assume_audio
        )
        if not rendi_scene_voice_url and FFmpegProcessor.check_ffmpeg_installed():
            rendi_scene_voice_url = LocalFFmpegFallback.add_music_to_video(
                processor.gcs_storage_service, concat_video_url, music_url, 0.2, assume_has_audio=assume_audio
            )
    elif has_single_vo:
        if has_music:
            rendi_scene_voice_url = processor.rendi_service.add_vo_and_music_to_video(
                concat_video_url, vo_audio_url, music_url, 1.0, 0.2
            )
        else:
            rendi_scene_voice_url = processor.rendi_service.add_audio_to_video(concat_video_url, vo_audio_url)
            if not rendi_scene_voice_url and FFmpegProcessor.check_ffmpeg_installed():
                rendi_scene_voice_url = LocalFFmpegFallback.add_audio_to_video(
                    processor.gcs_storage_service, concat_video_url, vo_audio_url
                )
    elif has_music:
        rendi_scene_voice_url = processor.rendi_service.add_background_music_to_video(
            concat_video_url, music_url, 0.3
        )
        if not rendi_scene_voice_url and FFmpegProcessor.check_ffmpeg_installed():
            rendi_scene_voice_url = LocalFFmpegFallback.add_music_to_video(
                processor.gcs_storage_service, concat_video_url, music_url, 0.3
            )

    if rendi_scene_voice_url:
        try:
            processor.sheets_service.update_cell(
                config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                config.RENDI_SCENE_VOICE_COLUMN, rendi_scene_voice_url, headers
            )
        except Exception:
            pass

    final_video_for_output = rendi_scene_voice_url or concat_video_url
    if add_subtitles and processor.zapcap_service and final_video_for_output:
        try:
            _zapcap_transcript_fb = vo_result.get("word_segments") if vo_result else None
            # Enrich transcript with emoji + importance markers
            _subtitle_enrichments = None
            _sheet_subtitle_emoji = get_pipeline_defaults().get("subtitle_emoji", True)
            if _sheet_subtitle_emoji and _zapcap_transcript_fb:
                try:
                    _normalized_for_enrich = processor.zapcap_service._normalize_transcript_for_zapcap(_zapcap_transcript_fb)
                    if _normalized_for_enrich:
                        _subtitle_enrichments = enrich_transcript_for_subtitles(
                            lambda msgs, **kw: processor._call_llm("enrich_subtitles", msgs, **kw),
                            word_segments=_normalized_for_enrich,
                            vo_script=vo_result.get("script", "") if vo_result else "",
                            language=subtitle_language,
                            fallback_call_fn=lambda msgs, **kw: processor._call_llm("enrich_subtitles_fallback", msgs, **kw),
                        )
                except Exception as _enrich_err:
                    logger.warning(f"   [Row {row_num}] Subtitle enrichment failed (non-blocking): {_enrich_err}")
            subtitled_video_url = processor.zapcap_service.add_subtitles(
                video_url=final_video_for_output, language=subtitle_language,
                transcript=_zapcap_transcript_fb,
                enrichments=_subtitle_enrichments,
            )
            if subtitled_video_url:
                subtitled_video_url = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video_url)
            if subtitled_video_url:
                gcs_key = f"Comp/Final_Video/product_videos/row_{row_num}_subtitled_{int(time.time())}.mp4"
                gcs_sub = processor.gcs_storage_service.upload_video_from_url(subtitled_video_url, gcs_key)
                final_video_for_output = gcs_sub or subtitled_video_url
                try:
                    processor.sheets_service.update_cell(
                        config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                        config.SUBTITLED_VIDEO_COLUMN, final_video_for_output, headers
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"   [Row {row_num}] ZapCap error: {e}")

    try:
        processor.sheets_service.update_cell(
            config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
            config.FINAL_VIDEO_COLUMN, final_video_for_output, headers
        )
    except Exception:
        pass

    result["final_video_url"] = final_video_for_output
    result["success"] = True
    return result


def run_rendi_voice_subtitles_for_row(processor, row_num: int) -> Optional[str]:
    """One-off: read scene videos, VO, music from sheet for one row; produce RENDI Scene, RENDI Scene & Voice, and Subtitled Video.

    Row number is passed as argument (not hardcoded). Reads: Scene 1 - new video, Scene 2 - new video, ..., New Voice, New music, Language.
    """
    logger.info(f"One-off run for row {row_num}: RENDI Scene -> RENDI Scene & Voice -> Subtitled Video")
    headers, data_rows = processor.sheets_service.get_worksheet_data(
        config.GOOGLE_SHEET_ID,
        config.GOOGLE_SHEET_TAB
    )
    row_index = row_num - 2
    if row_index < 0 or row_index >= len(data_rows):
        logger.error(f"Row {row_num} not found (sheet has {len(data_rows)} data rows)")
        return None
    row_data = data_rows[row_index]
    scene_videos = []
    for n in range(1, config.MAX_SCENES + 1):
        try:
            col_idx = processor.sheets_service.get_column_index(
                headers, config.SCENE_NEW_VIDEO_PREFIX.format(n=n)
            )
        except ValueError:
            break
        if col_idx >= len(row_data):
            break
        url = (row_data[col_idx] or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            break
        scene_videos.append(url)
    if not scene_videos:
        logger.error(f"Row {row_num}: no 'Scene N - new video' URLs found")
        return None
    logger.info(f"   Row {row_num}: found {len(scene_videos)} scene video(s)")
    vo_url = None
    try:
        vo_col = processor.sheets_service.get_column_index(headers, config.NEW_VOICE_COLUMN)
        if vo_col < len(row_data):
            vo_url = (row_data[vo_col] or "").strip()
            if vo_url and not (vo_url.startswith("http://") or vo_url.startswith("https://")):
                vo_url = None
    except ValueError:
        pass
    music_url = None
    try:
        music_col = processor.sheets_service.get_column_index(headers, config.NEW_MUSIC_COLUMN)
        if music_col < len(row_data):
            music_url = (row_data[music_col] or "").strip()
            if music_url and not (music_url.startswith("http://") or music_url.startswith("https://")):
                music_url = None
    except ValueError:
        pass
    try:
        lang_col = processor.sheets_service.get_column_index(headers, config.LANGUAGE_COLUMN)
        subtitle_language = (row_data[lang_col] or "").strip().lower() if lang_col < len(row_data) else "en"
    except ValueError:
        subtitle_language = "en"
    if not subtitle_language:
        subtitle_language = "en"
    durations = []
    with ThreadPoolExecutor(max_workers=min(8, len(scene_videos))) as ex:
        futures = {ex.submit(processor.rendi_service.get_video_duration_cloud, u): i for i, u in enumerate(scene_videos)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                d = fut.result()
                durations.append((i, d if d and d > 0 else 5.0))
            except Exception:
                durations.append((i, 5.0))
    durations.sort(key=lambda x: x[0])
    duration_list = [d for _, d in durations]
    video_data = [{"video_url": u, "duration": duration_list[i]} for i, u in enumerate(scene_videos)]
    # Cap every clip to actual file length so no clip freezes (same as UGC flow)
    def _probe(ent):
        try:
            return processor.rendi_service.get_video_duration_cloud(ent["video_url"])
        except Exception:
            return 0.0
    with ThreadPoolExecutor(max_workers=min(8, len(video_data))) as ex:
        futures = {ex.submit(_probe, v): i for i, v in enumerate(video_data)}
        for fut in as_completed(futures):
            i = futures[fut]
            ent = video_data[i]
            req = ent.get("duration") or 5.0
            try:
                actual = fut.result()
            except Exception:
                actual = 0.0
            if actual > 0 and req > actual:
                ent["duration"] = round(actual, 2)
                logger.info(f"   Row {row_num}: Clip {i+1} capped {req:.1f}s -> {ent['duration']:.1f}s (avoids frozen frame)")
    concat_video_url = processor.rendi_service.concatenate_videos(
        video_data=video_data,
        video_only=True,
        dissolve_seconds=getattr(config, "CONCAT_DISSOLVE_SECONDS", 0.075)
    )
    if not concat_video_url:
        logger.error("RENDI concat failed")
        return None
    try:
        processor.sheets_service.update_cell(
            config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
            config.RENDI_SCENE_COLUMN, concat_video_url, headers
        )
        logger.info(f"   Row {row_num}: RENDI Scene written")
    except Exception as e:
        logger.warning(f"   Update RENDI Scene failed: {e}")
    rendi_scene_voice_url = concat_video_url
    if vo_url and music_url:
        rendi_scene_voice_url = processor.rendi_service.add_vo_and_music_to_video(
            concat_video_url, vo_url, music_url, 1.0, 0.2
        )
    elif vo_url:
        rendi_scene_voice_url = processor.rendi_service.add_audio_to_video(concat_video_url, vo_url)
    elif music_url:
        rendi_scene_voice_url = processor.rendi_service.add_background_music_to_video(concat_video_url, music_url, 0.3)
    if rendi_scene_voice_url:
        try:
            processor.sheets_service.update_cell(
                config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                config.RENDI_SCENE_VOICE_COLUMN, rendi_scene_voice_url, headers
            )
            logger.info(f"   Row {row_num}: RENDI Scene & Voice written")
        except Exception as e:
            logger.warning(f"   Update RENDI Scene & Voice failed: {e}")
    else:
        logger.warning("   Could not add VO/music; using RENDI Scene as Scene & Voice")
        rendi_scene_voice_url = concat_video_url
    if not processor.zapcap_service:
        logger.warning("   ZapCap not available; skipping Subtitled Video")
        final_url = rendi_scene_voice_url
    else:
        try:
            subtitled_url = processor.zapcap_service.add_subtitles(
                video_url=rendi_scene_voice_url,
                language=subtitle_language,
                transcript=None
            )
        except Exception as e:
            logger.warning(f"   ZapCap failed: {e}")
            subtitled_url = None
        if not subtitled_url:
            final_url = rendi_scene_voice_url
        else:
            gcs_key = f"Comp/Final_Video/ugc_videos/row_{row_num}_subtitled_{int(time.time())}.mp4"
            gcs_url = processor.gcs_storage_service.upload_video_from_url(
                source_url=subtitled_url, key_name=gcs_key
            )
            final_url = gcs_url or subtitled_url
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                    config.SUBTITLED_VIDEO_COLUMN, final_url, headers
                )
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                    config.FINAL_VIDEO_COLUMN, final_url, headers
                )
                logger.info(f"   Row {row_num}: Subtitled Video and Final Video written")
            except Exception as e:
                logger.warning(f"   Update Subtitled/Final Video failed: {e}")
    logger.info(f"   Row {row_num}: Done. Final URL: {final_url[:60]}...")
    return final_url


def add_subtitles_to_row_from_rendi_voice(processor, row_num: int) -> Optional[str]:
    """Take the existing 'RENDI Scene & Voice' video for one row, add subtitles via ZapCap, upload to GCS, and write Final Video.

    No regeneration: no new images, animations, or VO. Only: ZapCap subtitles -> GCS -> update sheet.

    Args:
        processor: VideoSceneProcessor instance.
        row_num: Sheet row number (e.g. 4).

    Returns:
        Final video URL (with subtitles) if successful, None otherwise.
    """
    logger.info(f"Subtitles-only run for row {row_num}: starting from RENDI Scene & Voice...")
    if not processor.zapcap_service:
        logger.error("ZapCap service not available (no API key). Cannot add subtitles.")
        return None
    headers, data_rows = processor.sheets_service.get_worksheet_data(
        config.GOOGLE_SHEET_ID,
        config.GOOGLE_SHEET_TAB
    )
    row_index = row_num - 2
    if row_index < 0 or row_index >= len(data_rows):
        logger.error(f"Row {row_num} not found (data has {len(data_rows)} rows)")
        return None
    row_data = data_rows[row_index]
    try:
        rendi_voice_col = processor.sheets_service.get_column_index(headers, config.RENDI_SCENE_VOICE_COLUMN)
    except ValueError:
        logger.error(f"Column '{config.RENDI_SCENE_VOICE_COLUMN}' not found")
        return None
    if rendi_voice_col >= len(row_data):
        logger.error(f"Row {row_num}: no value for RENDI Scene & Voice")
        return None
    rendi_url = (row_data[rendi_voice_col] or "").strip()
    if not rendi_url or not (rendi_url.startswith("http://") or rendi_url.startswith("https://")):
        logger.error(f"Row {row_num}: RENDI Scene & Voice is empty or not a URL: '{rendi_url[:50] if rendi_url else ''}'")
        return None
    try:
        language_col = processor.sheets_service.get_column_index(headers, config.LANGUAGE_COLUMN)
        subtitle_language = (row_data[language_col] or "").strip().lower() if language_col < len(row_data) else "en"
    except ValueError:
        subtitle_language = "en"
    if not subtitle_language:
        subtitle_language = "en"
    logger.info(f"   Row {row_num}: source={rendi_url[:60]}..., language={subtitle_language}")
    try:
        subtitled_video_url = processor.zapcap_service.add_subtitles(
            video_url=rendi_url,
            language=subtitle_language,
            transcript=None
        )
        if subtitled_video_url:
            subtitled_video_url = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video_url)
    except Exception as e:
        logger.error(f"ZapCap add_subtitles failed: {e}")
        return None
    if not subtitled_video_url:
        logger.error("ZapCap returned no URL (timeout or task failed)")
        return None
    logger.info(f"   Row {row_num}: ZapCap subtitles OK, uploading to GCS...")
    gcs_key = f"Comp/Final_Video/ugc_videos/row_{row_num}_subtitled_{int(time.time())}.mp4"
    gcs_url = processor.gcs_storage_service.upload_video_from_url(
        source_url=subtitled_video_url,
        key_name=gcs_key
    )
    final_url = gcs_url or subtitled_video_url
    if gcs_url:
        logger.info(f"   Row {row_num}: Uploaded to GCS: {gcs_url[:60]}...")
    try:
        processor.sheets_service.update_cell(
            config.GOOGLE_SHEET_ID,
            config.GOOGLE_SHEET_TAB,
            row_num,
            config.FINAL_VIDEO_COLUMN,
            final_url,
            headers
        )
        try:
            processor.sheets_service.update_cell(
                config.GOOGLE_SHEET_ID,
                config.GOOGLE_SHEET_TAB,
                row_num,
                config.SUBTITLED_VIDEO_COLUMN,
                final_url,
                headers
            )
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"   Could not update sheet: {e}")
    logger.info(f"   Row {row_num}: Final Video (with subtitles) written to sheet: {final_url[:60]}...")
    return final_url


def process_influencer_row(
    processor,
    row_num: int,
    row_data: List[str],
    headers: List[str],
    free_text: str,
    manual_instructions: str = "",
    language: str = "",
    cta_button: bool = False,
    cta_text: str = "",
    cta_duration: str = "at_the_end",
    add_subtitles: bool = False,
    manual_vo_text: str = "",
    manual_music_link: str = "",
    image_urls: List[str] = None,
    scene_count: int = None,
    voice_id: str = "",
    gender: str = "f"
) -> Dict[str, Any]:
    """Process a row in influencer mode (no input video, generate from Free text).

    Creates an influencer-style recommendation video based on Free text content.
    """
    import tempfile
    from tvd_pipeline.utils import detect_language

    result = {
        "row": row_num,
        "success": False,
        "mode": "influencer",
        "scenes_processed": 0,
        "errors": [],
        "final_video_url": None
    }

    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"[Row {row_num}] INFLUENCER MODE - Generating recommendation video")
        logger.info(f"{'='*60}")

        # Set defaults
        scene_count = scene_count or config.DEFAULT_INFLUENCER_SCENES
        image_urls = image_urls or []

        # Step 1: Detect and set language
        if not language:
            language = detect_language(free_text)
            logger.info(f"[Row {row_num}] Detected language: {language}")
            processor._update_sheet_cell(row_num, config.LANGUAGE_COLUMN, language, headers)

        logger.info(f"[Row {row_num}] Free text: {len(free_text)} chars")
        logger.info(f"[Row {row_num}] Scene count: {scene_count}")
        logger.info(f"[Row {row_num}] Reference images: {len(image_urls)}")
        logger.info(f"[Row {row_num}] CTA button: {cta_button}, CTA text: '{cta_text}'")
        logger.info(f"[Row {row_num}] Add subtitles: {add_subtitles}")

        # Step 2: Download and analyze reference images
        reference_images = []
        for i, img_url in enumerate(image_urls):
            if img_url:
                try:
                    logger.info(f"[Row {row_num}] Downloading reference image {i+1}...")
                    response = requests.get(img_url, timeout=30)
                    response.raise_for_status()

                    # Convert to base64 for OpenAI analysis
                    img_base64 = base64.b64encode(response.content).decode('utf-8')

                    # Get image analysis via _call_llm
                    call_fn_img = lambda msgs, **kw: processor._call_llm("generate_image_prompt", msgs, **kw)
                    analysis_result = _generate_image_prompt(
                        call_fn_img,
                        image_contents=[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}]
                    )

                    reference_images.append({
                        "index": i + 1,
                        "url": img_url,
                        "base64": img_base64,
                        "analysis": analysis_result.get("analysis", "")[:500] if analysis_result else ""
                    })
                    logger.info(f"[Row {row_num}] Reference image {i+1} analyzed")
                except Exception as e:
                    logger.warning(f"[Row {row_num}] Could not process reference image {i+1}: {e}")
                    reference_images.append({"index": i + 1, "url": img_url, "analysis": ""})

        # Step 3: Generate influencer prompts via _call_llm
        logger.info(f"[Row {row_num}] Generating influencer prompts...")
        call_fn_prompts = lambda msgs, **kw: processor._call_llm("generate_scenes", msgs, **kw)
        prompts_result = generate_influencer_prompts(
            call_fn_prompts,
            free_text=free_text,
            reference_images=reference_images,
            scene_count=scene_count,
            manual_instructions=manual_instructions,
            cta_text=cta_text,
            language=language
        )

        scene_prompts = prompts_result.get("scene_prompts", [])
        influencer_description = prompts_result.get("influencer_description", "")

        if not scene_prompts:
            raise Exception("Failed to generate influencer prompts")

        logger.info(f"[Row {row_num}] Generated {len(scene_prompts)} scene prompts")

        # Step 4: Write prompts to sheet and generate images/videos (scenes in parallel, within rate limits)
        scene_videos = []
        scene_durations = []

        # Process scenes in parallel (independent operations; row-level remains sequential)
        with ThreadPoolExecutor(max_workers=min(scene_count, 7)) as executor:
            futures = {}

            for prompt_data in scene_prompts:
                scene_num = prompt_data.get("scene_number", 1)
                first_prompt = prompt_data.get("first_prompt", "")
                second_prompt = prompt_data.get("second_prompt", "")
                ref_image_index = prompt_data.get("reference_image_index")

                # Write prompts to sheet
                processor._update_sheet_cell(
                    row_num,
                    config.SCENE_FIRST_PROMPT_PREFIX.format(n=scene_num),
                    first_prompt,
                    headers
                )
                processor._update_sheet_cell(
                    row_num,
                    config.SCENE_SECOND_PROMPT_PREFIX.format(n=scene_num),
                    second_prompt,
                    headers
                )

                # Get reference image for this scene (cycling through available images)
                ref_url = None
                ref_desc = None
                if reference_images:
                    img_index = (scene_num - 1) % len(reference_images)
                    ref_img = reference_images[img_index]
                    ref_url = ref_img.get("url")
                    ref_desc = ref_img.get("analysis", "")[:300]

                future = executor.submit(
                    _process_influencer_scene,
                    processor,
                    scene_num=scene_num,
                    first_prompt=first_prompt,
                    second_prompt=second_prompt,
                    reference_image_url=ref_url,
                    reference_description=ref_desc,
                    row_num=row_num,
                    headers=headers
                )
                futures[future] = scene_num

            for future in as_completed(futures):
                scene_num = futures[future]
                try:
                    scene_result = future.result()
                    if scene_result.get("video_url"):
                        scene_videos.append({
                            "scene_num": scene_num,
                            "video_url": scene_result["video_url"],
                            "duration": config.INFLUENCER_SCENE_DURATION
                        })
                        scene_durations.append(config.INFLUENCER_SCENE_DURATION)
                        result["scenes_processed"] += 1
                        logger.info(f"[Row {row_num}] Scene {scene_num} completed")
                    else:
                        result["errors"].append(f"Scene {scene_num}: No video generated")
                except Exception as e:
                    result["errors"].append(f"Scene {scene_num}: {str(e)}")
                    logger.error(f"[Row {row_num}] Scene {scene_num} failed: {e}")

        scene_videos.sort(key=lambda x: x["scene_num"])
        video_urls = [s["video_url"] for s in scene_videos]

        if not video_urls:
            raise Exception("No scene videos were generated")

        # Step 5: Concatenate videos with smooth dissolve between shots (video_only; VO added in Step 6)
        logger.info(f"[Row {row_num}] Concatenating {len(video_urls)} scene videos with smooth transitions...")
        video_data = [{"video_url": u, "duration": config.INFLUENCER_SCENE_DURATION} for u in video_urls]
        combined_video = processor.rendi_service.concatenate_videos(
            video_data=video_data,
            video_only=True,
            dissolve_seconds=getattr(config, "CONCAT_DISSOLVE_SECONDS", 0.075)
        )

        if not combined_video:
            raise Exception("Failed to concatenate videos")

        processor._update_sheet_cell(row_num, config.RENDI_SCENE_COLUMN, combined_video, headers)
        logger.info(f"[Row {row_num}] Videos concatenated: {combined_video}")

        # Calculate total video duration
        total_video_duration = len(video_urls) * config.INFLUENCER_SCENE_DURATION

        # Step 6: Generate VO
        logger.info(f"[Row {row_num}] Generating voice-over...")

        if manual_vo_text:
            vo_script = manual_vo_text
            logger.info(f"[Row {row_num}] Using manual VO text")
        else:
            call_fn_vo = lambda msgs, **kw: processor._call_llm("generate_vo", msgs, **kw)
            vo_script = generate_influencer_vo_script(
                call_fn_vo,
                free_text=free_text,
                arc_beats="",
                target_duration=total_video_duration,
                manual_instructions=manual_instructions,
                language=language
            )

        # Generate TTS with timestamps for precise subtitle synchronization
        # Voice selection: Voice id column (voice_id param) -> random from catalog -> default
        if voice_id and voice_id.strip():
            tts_voice_id = voice_id.strip()
        else:
            gender_label = "female" if gender == "f" else "male"
            tts_voice_id = processor.elevenlabs_service.pick_random_voice(gender=gender_label, language=language)
            if not tts_voice_id:
                tts_voice_id = config.DEFAULT_FEMALE_VOICE_ID if gender == "f" else config.DEFAULT_VOICE_ID
                logger.info(f"[Row {row_num}] Fallback to default {gender_label} voice: {tts_voice_id}")
        logger.info(f"[Row {row_num}] Using voice ID: {tts_voice_id}")

        tts_result = processor.elevenlabs_service.text_to_speech_with_timestamps(
            text=vo_script,
            voice_id=tts_voice_id,
            language=language
        )

        # Store word segments for ZapCap
        word_segments = []

        if tts_result:
            voice_audio, word_segments = tts_result
            logger.info(f"[Row {row_num}] Got {len(word_segments)} word segments from TTS")

            # Upload voice to GCS
            timestamp = int(time.time())
            voice_key = f"influencer_voice_row_{row_num}_{timestamp}.mp3"
            voice_url = processor.gcs_storage_service.upload_audio_bytes(voice_audio, voice_key)

            if voice_url:
                processor._update_sheet_cell(row_num, config.NEW_VOICE_COLUMN, voice_url, headers)
                logger.info(f"[Row {row_num}] Voice generated: {voice_url}")
            else:
                raise Exception("Failed to upload voice to GCS")
        else:
            raise Exception("Failed to generate voice")

        # Step 7: Generate or use manual music
        if manual_music_link:
            music_url = manual_music_link
            logger.info(f"[Row {row_num}] Using manual music link")
        else:
            logger.info(f"[Row {row_num}] Generating background music...")
            call_fn_music = lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw)
            music_description = generate_music_description_from_text(call_fn_music, content_text=free_text[:1000])
            # Use pure music generation (no reference audio) to avoid copyright issues
            music_url = processor.suno_service.generate_pure_music(
                style_description=music_description
            )

        if music_url:
            processor._update_sheet_cell(row_num, config.NEW_MUSIC_COLUMN, music_url, headers)
            logger.info(f"[Row {row_num}] Music ready: {music_url}")

        # Step 8: Combine video with voice
        logger.info(f"[Row {row_num}] Adding voice to video...")
        video_with_voice = processor.rendi_service.add_audio_to_video(
            video_url=combined_video,
            audio_url=voice_url
        )

        if video_with_voice:
            processor._update_sheet_cell(row_num, config.RENDI_SCENE_VOICE_COLUMN, video_with_voice, headers)
            logger.info(f"[Row {row_num}] Voice added: {video_with_voice}")
        else:
            raise Exception("Failed to add voice to video")

        # Step 9: Add background music
        final_video = video_with_voice  # Default to voice-only
        if music_url:
            logger.info(f"[Row {row_num}] Adding background music (volume: 0.2)...")
            video_with_music = processor.rendi_service.add_background_music_to_video(
                video_url=video_with_voice,
                music_url=music_url,
                music_volume=0.2  # Lower volume for background music
            )
            if video_with_music:
                final_video = video_with_music
                logger.info(f"[Row {row_num}] Background music added: {final_video}")
            else:
                logger.warning(f"[Row {row_num}] Failed to add music, using voice-only version")

        # Step 9.5: Add CTA button overlay if requested
        if cta_button and cta_text:
            # Determine CTA timing based on cta_duration setting
            if cta_duration == "whole_video":
                logger.info(f"[Row {row_num}] Adding CTA button overlay for WHOLE VIDEO: '{cta_text}'...")
            else:
                logger.info(f"[Row {row_num}] Adding CTA button overlay to last scene: '{cta_text}'...")

            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    # Step 1: Generate CTA button image
                    cta_image_url = processor.kie_service.generate_cta_button(cta_text)

                    if cta_image_url:
                        logger.info(f"[Row {row_num}] CTA button image generated: {cta_image_url[:50]}...")

                        # Step 2: Process CTA button (remove green background)
                        cta_processed_url = processor._process_cta_button(
                            cta_image_url=cta_image_url,
                            temp_dir=temp_dir,
                            row_num=row_num
                        )

                        if cta_processed_url:
                            # Step 3: Overlay CTA button on video
                            if cta_duration == "whole_video":
                                # CTA appears for the entire video
                                cta_start_time = 0.0
                                cta_end_time = total_video_duration
                                logger.info(f"   CTA will appear from 0.0s to {total_video_duration:.1f}s (whole video)")
                            else:
                                # CTA appears only in the last scene (last 5 seconds)
                                cta_start_time = max(0, total_video_duration - 5.0)
                                cta_end_time = total_video_duration
                                logger.info(f"   CTA will appear from {cta_start_time:.1f}s to {total_video_duration:.1f}s (last scene)")

                            video_with_cta = processor.rendi_service.overlay_cta_on_video_timed(
                                video_url=final_video,
                                cta_image_url=cta_processed_url,
                                position="center",
                                start_time=cta_start_time,
                                end_time=cta_end_time
                            )

                            if video_with_cta:
                                final_video = video_with_cta
                                if cta_duration == "whole_video":
                                    logger.info(f"[Row {row_num}] CTA button added for whole video")
                                else:
                                    logger.info(f"[Row {row_num}] CTA button added to last scene (starts at {cta_start_time:.1f}s)")
                            else:
                                logger.warning(f"[Row {row_num}] Failed to overlay CTA button")
                        else:
                            logger.warning(f"[Row {row_num}] Failed to process CTA button image")
                    else:
                        logger.warning(f"[Row {row_num}] Failed to generate CTA button image")
            except Exception as e:
                logger.warning(f"[Row {row_num}] CTA button overlay failed: {e}")

        # Step 10: Add subtitles if requested
        subtitled_video = None
        if add_subtitles and processor.zapcap_service:
            logger.info(f"[Row {row_num}] Adding subtitles...")
            # Enrich transcript with emoji + importance markers
            _subtitle_enrichments = None
            _sheet_subtitle_emoji2 = get_pipeline_defaults().get("subtitle_emoji", True)
            if _sheet_subtitle_emoji2 and word_segments:
                try:
                    _normalized_for_enrich = processor.zapcap_service._normalize_transcript_for_zapcap(word_segments)
                    if _normalized_for_enrich:
                        _subtitle_enrichments = enrich_transcript_for_subtitles(
                            lambda msgs, **kw: processor._call_llm("enrich_subtitles", msgs, **kw),
                            word_segments=_normalized_for_enrich,
                            vo_script=vo_script if vo_script else "",
                            language=language,
                            fallback_call_fn=lambda msgs, **kw: processor._call_llm("enrich_subtitles_fallback", msgs, **kw),
                        )
                except Exception as _enrich_err:
                    logger.warning(f"[Row {row_num}] Subtitle enrichment failed (non-blocking): {_enrich_err}")
            # Pass word segments for precise timing (Bring Your Own Transcript)
            subtitled_video = processor.zapcap_service.add_subtitles(
                video_url=final_video or video_with_voice,
                language=language,
                transcript=word_segments if word_segments else None,
                enrichments=_subtitle_enrichments,
            )
            if subtitled_video:
                subtitled_video = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video)
            if subtitled_video:
                processor._update_sheet_cell(row_num, config.SUBTITLED_VIDEO_COLUMN, subtitled_video, headers)
                logger.info(f"[Row {row_num}] Subtitles added with precise timing")

        # Step 11: Upload final video to GCS
        logger.info(f"[Row {row_num}] Uploading final video to GCS...")
        source_video = subtitled_video or final_video or video_with_voice

        timestamp = int(time.time())
        final_gcs_url = processor.gcs_video_service.upload_video_from_url(
            source_url=source_video,
            key_name=f"influencer_final_row_{row_num}_{timestamp}.mp4",
            folder="influencer_videos"
        )

        if final_gcs_url:
            processor._update_sheet_cell(row_num, config.FINAL_VIDEO_COLUMN, final_gcs_url, headers)
            result["final_video_url"] = final_gcs_url
            result["success"] = True
            logger.info(f"[Row {row_num}] Final video uploaded to GCS: {final_gcs_url}")
        else:
            raise Exception("Failed to upload final video to GCS")

        logger.info(f"\n[Row {row_num}] Influencer video completed successfully!")

    except Exception as e:
        result["errors"].append(str(e))
        logger.error(f"[Row {row_num}] Influencer mode failed: {e}")

    return result


def _process_influencer_scene(
    processor,
    scene_num: int,
    first_prompt: str,
    second_prompt: str,
    reference_image_url: Optional[str],
    reference_description: Optional[str],
    row_num: int,
    headers: List[str],
    animation_model: str = "runway",
    target_language: str = "en"
) -> Dict[str, Any]:
    """Process a single influencer scene (image + video generation).

    Args:
        processor: VideoSceneProcessor instance.
        scene_num: Scene number.
        first_prompt: Image generation prompt.
        second_prompt: Motion/video prompt.
        reference_image_url: Optional reference image URL.
        reference_description: Optional reference image description.
        row_num: Row number for sheet updates.
        headers: Column headers.
        animation_model: Animation model to use ('runway' or 'kling').
        target_language: Target language for text on images.

    Returns:
        Dict with image_url and video_url.
    """
    result = {"scene_num": scene_num, "image_url": None, "video_url": None}

    try:
        # Generate image with Nano Banana
        logger.info(f"[Scene {scene_num}] Generating image...")
        image_url = processor.kie_service.generate_image_nano_banana(
            prompt=first_prompt,
            reference_image_url=reference_image_url,
            reference_description=reference_description,
            target_language=target_language
        )

        if image_url:
            result["image_url"] = image_url
            processor._update_sheet_cell(
                row_num,
                config.SCENE_NEW_IMAGE_PREFIX.format(n=scene_num),
                image_url,
                headers
            )

            # Generate video with Runway or Kling
            logger.info(f"[Scene {scene_num}] Generating video with {animation_model.upper()}...")
            if animation_model == "kling":
                video_url = processor.kie_service.generate_video_kling(
                    prompt=second_prompt,
                    image_url=image_url,
                    duration=config.INFLUENCER_SCENE_DURATION
                )
            else:
                video_url = processor.kie_service.generate_video_runway(
                    prompt=second_prompt,
                    image_url=image_url,
                    duration=config.INFLUENCER_SCENE_DURATION
                )

            if video_url:
                # Upload to GCS immediately to avoid temp URL expiration
                try:
                    gcs_video_url = processor._upload_video_to_gcs_from_url(
                        video_url=video_url,
                        row_num=row_num,
                        scene_num=scene_num
                    )
                    if gcs_video_url:
                        video_url = gcs_video_url
                        logger.info(f"[Scene {scene_num}] Video uploaded to GCS (permanent URL)")
                except Exception as upload_err:
                    logger.warning(f"[Scene {scene_num}] GCS upload failed, using temp URL: {upload_err}")

                result["video_url"] = video_url
                processor._update_sheet_cell(
                    row_num,
                    config.SCENE_NEW_VIDEO_PREFIX.format(n=scene_num),
                    video_url,
                    headers
                )
                logger.info(f"[Scene {scene_num}] Video generated")
            else:
                logger.warning(f"[Scene {scene_num}] Video generation failed")
        else:
            logger.warning(f"[Scene {scene_num}] Image generation failed")

    except Exception as e:
        logger.error(f"[Scene {scene_num}] Error: {e}")

    return result
