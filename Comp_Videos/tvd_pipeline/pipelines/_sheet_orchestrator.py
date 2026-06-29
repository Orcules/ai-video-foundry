"""Sheet orchestrator: process_all_videos extracted from VideoSceneProcessor.

Main Google Sheets loop that reads all rows from the worksheet, determines
the video type for each row, and dispatches to the correct pipeline method
on the processor instance.
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility helpers (duplicated from monolith top-level to keep this module
# self-contained; the monolith still has its own copies).
# ---------------------------------------------------------------------------

def _parse_character_urls(cell_value: str) -> List[str]:
    """Parse Character column value into a list of image URLs.

    Supports multiple URLs in one cell separated by comma or newline.
    """
    if not cell_value or not isinstance(cell_value, str):
        return []
    urls: List[str] = []
    for part in re.split(r"[\n,]", cell_value):
        s = part.strip()
        if not s:
            continue
        if s.startswith("http://") or s.startswith("https://") or s.startswith("gs://"):
            urls.append(s)
    return urls


def _normalize_animation_model_value(cell_value: str) -> str:
    """Normalize Animation model cell so 'Google 3.1' and variants match."""
    if not cell_value or not isinstance(cell_value, str):
        return ""
    s = cell_value.replace("\xa0", " ").replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _detect_language(text: str) -> str:
    """Detect language from text using langdetect or simple heuristics."""
    if not text:
        return ""
    try:
        from langdetect import detect as _ld_detect  # type: ignore
        return _ld_detect(text)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def process_all_videos(processor) -> Dict[str, Any]:
    """Process all videos from the Google Sheet.

    This is the main Google Sheets orchestration loop.  It reads the
    worksheet, resolves column indices, and dispatches each row to the
    correct pipeline method on *processor* (``VideoSceneProcessor``).

    Args:
        processor: A ``VideoSceneProcessor`` instance (used as ``self``
            in the original monolith method).

    Returns:
        Dict with keys ``processed``, ``successful``, ``failed``, ``details``.
    """
    config = processor.config if hasattr(processor, "config") else __import__("video_scene_processor").config

    logger.info("Starting video processing pipeline...")

    # Read data from Google Sheet
    headers, data_rows = processor.sheets_service.get_worksheet_data(
        config.GOOGLE_SHEET_ID,
        config.GOOGLE_SHEET_TAB,
    )

    # ------------------------------------------------------------------
    # Resolve optional column indices
    # ------------------------------------------------------------------
    def _col(col_name: str, label: str = "") -> Optional[int]:
        """Resolve a column index, returning None if not found."""
        try:
            return processor.sheets_service.get_column_index(headers, col_name)
        except ValueError:
            if label:
                logger.info(f"Column '{label}' not found, proceeding without")
            return None

    input_col = _col(config.INPUT_VIDEO_COLUMN, config.INPUT_VIDEO_COLUMN)
    manual_instructions_col = _col(config.MANUAL_INSTRUCTIONS_COLUMN, "Manual instructions")
    cta_button_col = _col(config.ADD_CTA_BUTTON_COLUMN, "CTA button")
    cta_text_col = _col(config.CTA_TEXT_COLUMN, "CTA text")
    cta_duration_col = _col(config.CTA_DURATION_COLUMN, "CTA Duration")
    add_subtitles_col = _col(config.ADD_SUBTITLES_COLUMN, "Add subtitles")
    add_opening_text_col = _col(config.ADD_OPENING_TEXT_COLUMN, "Opening Text?")
    opening_text_col = _col(config.OPENING_TEXT_COLUMN, "Opening Text")
    article_col = _col(config.ARTICLE_COLUMN, "Article")
    vertical_col = _col(config.VERTICAL_COLUMN, "Vertical")
    language_col = _col(config.LANGUAGE_COLUMN, "Language")
    manual_vo_text_col = _col(config.MANUAL_VO_TEXT_COLUMN, "Manual VO text")
    manual_music_link_col = _col(config.MANUAL_MUSIC_LINK_COLUMN, "Manual music link")
    free_text_col = _col(config.FREE_TEXT_COLUMN, "Free text")
    time_col = _col(config.TIME_COLUMN, "Time")
    voice_id_col = _col(config.VOICE_ID_COLUMN, "Voice id")
    animation_model_col = _col(config.ANIMATION_MODEL_COLUMN, "Animation model")
    title_col = _col(config.TITLE_COLUMN, "Title")
    first_para_col = _col(config.FIRST_PARAGRAPH_COLUMN, "1stP")
    rest_content_col = _col(config.REST_CONTENT_COLUMN, "Rest of Content")
    article_related_col = _col(config.ARTICLE_RELATED_TO_VIDEO_COLUMN, "Article related to Video")
    prompt_col = _col(config.PROMPT_COLUMN, "Prompt")
    vo_script_col = _col(config.VO_SCRIPT_COLUMN)
    style_col = _col(config.STYLE_COLUMN)
    duration_col = _col(config.DURATION_COLUMN)

    # Country column (with explicit log)
    country_col = _col(config.COUNTRY_COLUMN)
    if country_col is not None:
        logger.info("Found 'Country' column")

    # Video type column
    video_type_col = _col(config.VIDEO_TYPE_COLUMN)
    if video_type_col is not None:
        logger.info("Found 'Video type' column")
    else:
        logger.info("Video type column not found, using legacy workflow")

    # Final Video column (skip row if already filled)
    final_video_col = _col(config.FINAL_VIDEO_COLUMN)
    if final_video_col is not None:
        logger.info("Found 'Final Video' column (will skip rows that already have value)")

    # TEXT 1-4 output columns
    text_1_col = _col(config.TEXT_1_COLUMN)
    text_2_col = _col(config.TEXT_2_COLUMN)
    text_3_col = _col(config.TEXT_3_COLUMN)
    text_4_col = _col(config.TEXT_4_COLUMN)
    if any([text_1_col, text_2_col, text_3_col, text_4_col]):
        logger.info(
            f"   Found TEXT columns: TEXT 1={text_1_col is not None}, "
            f"TEXT 2={text_2_col is not None}, TEXT 3={text_3_col is not None}, "
            f"TEXT 4={text_4_col is not None}"
        )

    # Image 1-5 columns (influencer / UGC reference images)
    image_cols: List[Optional[int]] = []
    image_explain_cols: List[Optional[int]] = []
    for i in range(1, 6):
        col_name = getattr(config, f"IMAGE_{i}_COLUMN", f"Image {i}")
        img_col: Optional[int] = None
        try:
            img_col = processor.sheets_service.get_column_index(headers, col_name)
        except ValueError:
            lower_name = col_name.lower()
            for idx, h in enumerate(headers):
                if h.lower().strip() == lower_name:
                    img_col = idx
                    break
        if img_col is not None:
            logger.info(f"   Found '{col_name}' column at index {img_col}")
        image_cols.append(img_col)

        # Corresponding "Image X Explain" column
        explain_col: Optional[int] = None
        explain_name = f"image {i} Explain"
        for idx, h in enumerate(headers):
            if h.strip().lower() == explain_name.lower():
                explain_col = idx
                break
        if explain_col is not None:
            logger.info(f"   Found '{explain_name}' column at index {explain_col}")
        image_explain_cols.append(explain_col)

    # Asset 1-3 columns
    asset_cols: List[Optional[int]] = []
    for i in range(1, 4):
        col_name = getattr(config, f"ASSET_{i}_COLUMN", f"Asset {i}")
        try:
            asset_col = processor.sheets_service.get_column_index(headers, col_name)
            asset_cols.append(asset_col)
        except ValueError:
            asset_cols.append(None)

    # Product image columns (image 1-5, lowercase variant)
    product_image_cols: List[Optional[int]] = []
    for i in range(1, 6):
        col_name = getattr(config, f"PRODUCT_IMAGE_{i}_COLUMN", f"image {i}")
        img_col = None
        try:
            img_col = processor.sheets_service.get_column_index(headers, col_name)
        except ValueError:
            lower_name = col_name.lower()
            for idx, h in enumerate(headers):
                if h.lower().strip() == lower_name:
                    img_col = idx
                    break
        product_image_cols.append(img_col)

    # Character column (case-insensitive fallback)
    character_col: Optional[int] = None
    try:
        character_col = processor.sheets_service.get_column_index(headers, config.CHARACTER_COLUMN)
        logger.info("Found 'Character' column")
    except ValueError:
        for i, h in enumerate(headers):
            if h and str(h).strip().lower() == "character":
                character_col = i
                logger.info("Found 'Character' column (case-insensitive)")
                break

    # Character 2, Character 3 extra columns
    character_extra_cols: List[int] = []
    for col_name in getattr(config, "CHARACTER_EXTRA_COLUMNS", ()) or ():
        try:
            idx = processor.sheets_service.get_column_index(headers, col_name)
            character_extra_cols.append(idx)
            logger.info(f"Found '{col_name}' column")
        except ValueError:
            pass

    # Logo column
    logo_col = _col(config.LOGO_COLUMN)
    if logo_col is not None:
        logger.info("Found 'Logo' column")

    # Slogan column (with "Slogen" fallback and case-insensitive)
    slogan_col: Optional[int] = None
    for name in (config.SLOGAN_COLUMN, "Slogen"):
        try:
            slogan_col = processor.sheets_service.get_column_index(headers, name)
            logger.info(f"Found '{name}' column (slogan)")
            break
        except ValueError:
            pass
    if slogan_col is None:
        for i, h in enumerate(headers):
            if h and h.strip().lower() in ("slogan", "slogen"):
                slogan_col = i
                logger.info(f"Found slogan column at index {i} (case-insensitive)")
                break

    # Gender column
    gender_col = _col(config.GENDER_COLUMN)
    if gender_col is not None:
        logger.info("Found 'Gender' column")

    # Video reference column
    video_reference_col = _col(config.VIDEO_REFERENCE_COLUMN)
    if video_reference_col is not None:
        logger.info(f"Found '{config.VIDEO_REFERENCE_COLUMN}' column")

    # Product image mode column
    product_image_mode_col = processor._get_col_safe(headers, "Product image mode")
    if product_image_mode_col is not None:
        logger.info("Found 'Product image mode' column")

    # Dissolve column
    dissolve_col = processor._get_col_safe(headers, "Dissolve")
    if dissolve_col is not None:
        logger.info("Found 'Dissolve' column")

    # Style / Duration columns (log if present)
    if style_col is not None:
        logger.info("Found 'Style' column")
    if duration_col is not None:
        logger.info("Found 'Duration' column")

    # ------------------------------------------------------------------
    # Results accumulator
    # ------------------------------------------------------------------
    results: Dict[str, Any] = {
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "details": [],
    }

    # ------------------------------------------------------------------
    # Inner function: process a single row
    # ------------------------------------------------------------------
    def process_row(row_idx: int, row_data: List[str]) -> Optional[Dict[str, Any]]:
        """Process a single row. Rows are run sequentially."""
        row_num = row_idx + 2  # 1-based, accounting for header

        # Make a copy to avoid mutation issues
        row_data = list(row_data)

        # Pad row to header length
        while len(row_data) < len(headers):
            row_data.append("")

        # Skip if Final Video already filled
        if final_video_col is not None and final_video_col < len(row_data):
            existing_final = (row_data[final_video_col] or "").strip()
            if existing_final:
                logger.info(f"Row {row_num}: Skipping - Final Video already has value")
                return None

        # =============================================================
        # NEW VIDEO TYPE WORKFLOW
        # =============================================================
        if video_type_col is not None and video_type_col < len(row_data):
            video_type = row_data[video_type_col].strip().lower()

            if video_type == "product video":
                logger.info(f"Row {row_num}: Product video mode detected")

                prompt_text = ""
                if prompt_col is not None and prompt_col < len(row_data):
                    prompt_text = row_data[prompt_col].strip()

                if not prompt_text:
                    logger.warning(f"   Row {row_num}: Product video mode but no prompt provided")
                    return None

                # Product reference images
                product_images: List[str] = []
                for img_c in product_image_cols:
                    if img_c is not None and img_c < len(row_data):
                        img_url = row_data[img_c].strip()
                        if img_url:
                            product_images.append(img_url)

                # Animation model
                anim_model = "runway"
                if animation_model_col is not None and animation_model_col < len(row_data):
                    anim_value = _normalize_animation_model_value(row_data[animation_model_col])
                    anim_model = _resolve_animation_model(anim_value, row_num)

                # Visual style
                visual_style = "Auto"
                if style_col is not None and style_col < len(row_data):
                    style_value = row_data[style_col].strip()
                    if style_value and style_value in config.STYLE_OPTIONS:
                        visual_style = style_value
                        if visual_style != "Auto":
                            logger.info(f"   Row {row_num}: Using visual style: {visual_style}")

                # Duration
                target_duration = config.DEFAULT_VIDEO_DURATION
                if duration_col is not None and duration_col < len(row_data):
                    duration_value = row_data[duration_col].strip()
                    if duration_value:
                        try:
                            duration_int = int(duration_value)
                            if config.MIN_VIDEO_DURATION <= duration_int <= config.MAX_VIDEO_DURATION:
                                target_duration = duration_int
                                logger.info(f"   Row {row_num}: Target duration: {target_duration}s")
                            else:
                                logger.warning(
                                    f"   Row {row_num}: Duration {duration_int}s out of range "
                                    f"({config.MIN_VIDEO_DURATION}-{config.MAX_VIDEO_DURATION}), "
                                    f"using default {target_duration}s"
                                )
                        except ValueError:
                            logger.warning(f"   Row {row_num}: Invalid duration '{duration_value}', using default {target_duration}s")

                # Character URLs
                character_urls = _collect_character_urls(row_data, character_col, character_extra_cols, row_num)

                # Logo URL
                logo_url = _get_url_from_col(row_data, logo_col, row_num)

                # Slogan text
                slogan_text = None
                if slogan_col is not None and slogan_col < len(row_data):
                    slogan_value = row_data[slogan_col].strip()
                    if slogan_value:
                        slogan_text = slogan_value
                        if len(slogan_text) > 50:
                            logger.info(f"   Row {row_num}: Slogan provided: '{slogan_text[:50]}...' ")
                        else:
                            logger.info(f"   Row {row_num}: Slogan provided: '{slogan_text}'")

                # Video reference URL
                video_reference_url = _get_url_from_col(row_data, video_reference_col, row_num, log_label="Video reference URL")

                # Add subtitles
                add_subtitles = False
                if add_subtitles_col is not None and add_subtitles_col < len(row_data):
                    add_subtitles = row_data[add_subtitles_col].strip().lower() == "yes"
                    if add_subtitles:
                        logger.info(f"   Row {row_num}: Subtitles will be added")

                # Language
                subtitle_language = "en"
                if language_col is not None and language_col < len(row_data):
                    lang_value = row_data[language_col].strip().lower()
                    if lang_value:
                        subtitle_language = lang_value

                # Country
                country = ""
                if country_col is not None and country_col < len(row_data):
                    country = row_data[country_col].strip()

                # Product image mode
                product_image_mode = "none"
                if product_image_mode_col is not None and product_image_mode_col < len(row_data):
                    pim_value = row_data[product_image_mode_col].strip().lower()
                    if pim_value in ("auto", "clean", "force_clean", "none"):
                        product_image_mode = pim_value

                # Dissolve seconds
                dissolve_seconds = _parse_dissolve(row_data, dissolve_col)

                return processor.process_product_video(
                    row_num=row_num,
                    row_data=row_data,
                    headers=headers,
                    prompt=prompt_text,
                    image_urls=product_images,
                    text_1_col=text_1_col,
                    text_2_col=text_2_col,
                    text_3_col=text_3_col,
                    text_4_col=text_4_col,
                    vo_script_col=vo_script_col,
                    animation_model=anim_model,
                    generate_vo=True,
                    visual_style=visual_style,
                    target_duration=target_duration,
                    character_urls=character_urls,
                    logo_url=logo_url,
                    slogan_text=slogan_text,
                    add_subtitles=add_subtitles,
                    subtitle_language=subtitle_language,
                    video_reference_url=video_reference_url,
                    country=country,
                    product_image_mode=product_image_mode,
                    dissolve_seconds=dissolve_seconds,
                )

            elif video_type in ("ugc-style video", "influencer video", "personal brand video"):
                # UGC / Influencer / Personal Brand
                if video_type == "personal brand video":
                    logger.info(f"Row {row_num}: Personal Brand video mode detected")
                elif video_type == "influencer video":
                    logger.info(f"Row {row_num}: Influencer video mode detected")
                else:
                    logger.info(f"Row {row_num}: UGC-style video mode detected")

                prompt_text = ""
                if prompt_col is not None and prompt_col < len(row_data):
                    prompt_text = row_data[prompt_col].strip()

                if not prompt_text:
                    logger.warning(f"   Row {row_num}: UGC-style video but no prompt provided")
                    return None

                # Gender
                gender = "f"
                if gender_col is not None and gender_col < len(row_data):
                    gender_value = row_data[gender_col].strip().lower()
                    if gender_value in ["m", "male"]:
                        gender = "m"
                    elif gender_value in ["f", "female"]:
                        gender = "f"
                logger.info(f"   Row {row_num}: Gender: {'Female' if gender == 'f' else 'Male'}")

                # Reference images (Image 1-5, fallback to product image cols)
                # Also read existing explanations from "Image X Explain" columns
                reference_images: List[str] = []
                image_explanations: List[Optional[str]] = []
                for img_idx, img_c in enumerate(image_cols):
                    if img_c is not None and img_c < len(row_data):
                        img_url = row_data[img_c].strip()
                        if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
                            reference_images.append(img_url)
                            explain_c = image_explain_cols[img_idx] if img_idx < len(image_explain_cols) else None
                            explain_text = None
                            if explain_c is not None and explain_c < len(row_data):
                                explain_text = row_data[explain_c].strip() or None
                            image_explanations.append(explain_text)
                if not reference_images:
                    for img_c in product_image_cols:
                        if img_c is not None and img_c < len(row_data):
                            img_url = row_data[img_c].strip()
                            if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
                                reference_images.append(img_url)
                                image_explanations.append(None)
                logger.info(f"   Row {row_num}: {len(reference_images)} reference images provided")

                # Assets (Asset 1-3)
                assets: List[str] = []
                for asset_col_idx in asset_cols:
                    if asset_col_idx is not None and asset_col_idx < len(row_data):
                        asset_url = row_data[asset_col_idx].strip()
                        if asset_url and (asset_url.startswith("http://") or asset_url.startswith("https://")):
                            assets.append(asset_url)
                if assets:
                    logger.info(f"   Row {row_num}: {len(assets)} assets to insert as-is")

                # Character URLs
                character_urls = _collect_character_urls(row_data, character_col, character_extra_cols, row_num)

                # Animation model
                anim_model = "runway"
                if animation_model_col is not None and animation_model_col < len(row_data):
                    anim_value = _normalize_animation_model_value(row_data[animation_model_col])
                    anim_model = _resolve_animation_model(anim_value, row_num)

                # Visual style
                visual_style = "Auto"
                if style_col is not None and style_col < len(row_data):
                    style_value = row_data[style_col].strip()
                    if style_value and style_value in config.STYLE_OPTIONS:
                        visual_style = style_value

                # Duration
                target_duration = config.DEFAULT_VIDEO_DURATION
                if duration_col is not None and duration_col < len(row_data):
                    duration_value = row_data[duration_col].strip()
                    if duration_value:
                        try:
                            duration_int = int(duration_value)
                            if config.MIN_VIDEO_DURATION <= duration_int <= config.MAX_VIDEO_DURATION:
                                target_duration = duration_int
                        except ValueError:
                            pass

                # Logo
                logo_url = _get_url_from_col(row_data, logo_col, row_num)

                # Slogan
                slogan_text = None
                if slogan_col is not None and slogan_col < len(row_data):
                    slogan_value = row_data[slogan_col].strip()
                    if slogan_value:
                        slogan_text = slogan_value

                # Subtitles
                add_subtitles = False
                add_subtitles_raw = ""
                if add_subtitles_col is not None and add_subtitles_col < len(row_data):
                    add_subtitles_raw = (row_data[add_subtitles_col] or "").strip()
                    add_subtitles = add_subtitles_raw.lower() == "yes"
                logger.info(f"   Row {row_num}: Add subtitles = {add_subtitles} (column value: '{add_subtitles_raw or '(empty)'}')")

                # Language
                subtitle_language = "en"
                if language_col is not None and language_col < len(row_data):
                    lang_value = row_data[language_col].strip().lower()
                    if lang_value:
                        subtitle_language = lang_value

                # Country
                country = ""
                if country_col is not None and country_col < len(row_data):
                    country = row_data[country_col].strip()

                # Dissolve
                dissolve_seconds = _parse_dissolve(row_data, dissolve_col)

                subtype = "personal_brand" if video_type == "personal brand video" else "influencer"
                return processor.process_ugc_video(
                    row_num=row_num,
                    row_data=row_data,
                    headers=headers,
                    prompt=prompt_text,
                    gender=gender,
                    reference_images=reference_images,
                    image_explanations=image_explanations,
                    image_explain_cols=image_explain_cols,
                    assets=assets,
                    character_urls=character_urls,
                    text_1_col=text_1_col,
                    text_2_col=text_2_col,
                    text_3_col=text_3_col,
                    text_4_col=text_4_col,
                    vo_script_col=vo_script_col,
                    animation_model=anim_model,
                    visual_style=visual_style,
                    target_duration=target_duration,
                    logo_url=logo_url,
                    slogan_text=slogan_text,
                    add_subtitles=add_subtitles,
                    subtitle_language=subtitle_language,
                    character_col=character_col,
                    country=country,
                    video_subtype=subtype,
                    dissolve_seconds=dissolve_seconds,
                )

            elif video_type in config.VIDEO_TYPES:
                logger.info(f"Row {row_num}: Video type '{video_type}' not yet implemented, skipping")
                return None

        # =============================================================
        # LEGACY WORKFLOW - Input Videos based processing
        # =============================================================

        # Video URL from Input Videos column
        video_url = ""
        if input_col is not None and input_col < len(row_data):
            video_url = row_data[input_col].strip()

        # Free text (for influencer mode fallback)
        free_text = ""
        if free_text_col is not None and free_text_col < len(row_data):
            free_text = row_data[free_text_col].strip()

        if not video_url:
            if not free_text:
                return None  # Skip empty rows

            # INFLUENCER MODE (legacy) - no video URL but has Free text
            logger.info(f"Row {row_num}: Influencer mode detected (no Input Videos, has Free text)")

            manual_instructions = ""
            if manual_instructions_col is not None and manual_instructions_col < len(row_data):
                manual_instructions = row_data[manual_instructions_col].strip()

            cta_button = False
            if cta_button_col is not None and cta_button_col < len(row_data):
                cta_button = row_data[cta_button_col].strip().lower() == "yes"

            cta_text = ""
            if cta_text_col is not None and cta_text_col < len(row_data):
                cta_text = row_data[cta_text_col].strip()

            cta_duration = "at_the_end"
            if cta_duration_col is not None and cta_duration_col < len(row_data):
                duration_value = row_data[cta_duration_col].strip().lower()
                if "whole" in duration_value:
                    cta_duration = "whole_video"

            add_subtitles = False
            if add_subtitles_col is not None and add_subtitles_col < len(row_data):
                add_subtitles = row_data[add_subtitles_col].strip().lower() == "yes"

            language = ""
            if language_col is not None and language_col < len(row_data):
                language = row_data[language_col].strip()

            manual_vo_text = ""
            if manual_vo_text_col is not None and manual_vo_text_col < len(row_data):
                manual_vo_text = row_data[manual_vo_text_col].strip()

            manual_music_link = ""
            if manual_music_link_col is not None and manual_music_link_col < len(row_data):
                manual_music_link = row_data[manual_music_link_col].strip()

            # Image 1-4 URLs
            image_urls: List[str] = []
            for img_c in image_cols:
                if img_c is not None and img_c < len(row_data):
                    img_url = row_data[img_c].strip()
                    if img_url:
                        image_urls.append(img_url)

            # Scene count from Time column
            scene_count = config.DEFAULT_INFLUENCER_SCENES
            if time_col is not None and time_col < len(row_data):
                time_value = row_data[time_col].strip()
                if time_value.isdigit():
                    scene_count = min(int(time_value), config.MAX_SCENES)

            custom_voice_id = ""
            if voice_id_col is not None and voice_id_col < len(row_data):
                custom_voice_id = row_data[voice_id_col].strip()

            gender = "f"
            if gender_col is not None and gender_col < len(row_data):
                gender_value = row_data[gender_col].strip().lower()
                if gender_value in ["m", "male"]:
                    gender = "m"
                elif gender_value in ["f", "female"]:
                    gender = "f"

            return processor.process_influencer_row(
                row_num=row_num,
                row_data=row_data,
                headers=headers,
                free_text=free_text,
                manual_instructions=manual_instructions,
                language=language,
                cta_button=cta_button,
                cta_text=cta_text,
                cta_duration=cta_duration,
                add_subtitles=add_subtitles,
                manual_vo_text=manual_vo_text,
                manual_music_link=manual_music_link,
                image_urls=image_urls,
                scene_count=scene_count,
                voice_id=custom_voice_id,
                gender=gender,
            )

        # NORMAL VIDEO MODE (legacy) - process_single_video
        manual_instructions = ""
        if manual_instructions_col is not None and manual_instructions_col < len(row_data):
            manual_instructions = row_data[manual_instructions_col].strip()
            if manual_instructions:
                logger.info(f"Row {row_num}: Manual instructions found: {manual_instructions[:50]}...")

        cta_button = False
        if cta_button_col is not None and cta_button_col < len(row_data):
            cta_value = row_data[cta_button_col].strip().lower()
            cta_button = cta_value == "yes"

        cta_text = ""
        if cta_text_col is not None and cta_text_col < len(row_data):
            cta_text = row_data[cta_text_col].strip()

        cta_duration = "at_the_end"
        if cta_duration_col is not None and cta_duration_col < len(row_data):
            duration_value = row_data[cta_duration_col].strip().lower()
            if "whole" in duration_value:
                cta_duration = "whole_video"

        add_subtitles = False
        if add_subtitles_col is not None and add_subtitles_col < len(row_data):
            subtitles_value = row_data[add_subtitles_col].strip().lower()
            add_subtitles = subtitles_value == "yes"

        add_opening_text = False
        if add_opening_text_col is not None and add_opening_text_col < len(row_data):
            add_opening_text = row_data[add_opening_text_col].strip().lower() == "yes"

        opening_text = ""
        if opening_text_col is not None and opening_text_col < len(row_data):
            opening_text = row_data[opening_text_col].strip()

        # Animation model (legacy)
        animation_model = "runway"
        if animation_model_col is not None and animation_model_col < len(row_data):
            anim_value = _normalize_animation_model_value(row_data[animation_model_col])
            animation_model = _resolve_animation_model(anim_value, row_num)

        # Article content (GCS fetch + Title / 1stP / Rest of Content)
        existing_title = ""
        existing_first_para = ""
        existing_rest_content = ""
        if title_col is not None and title_col < len(row_data):
            existing_title = row_data[title_col].strip()
        if first_para_col is not None and first_para_col < len(row_data):
            existing_first_para = row_data[first_para_col].strip()
        if rest_content_col is not None and rest_content_col < len(row_data):
            existing_rest_content = row_data[rest_content_col].strip()

        article_text = ""
        article_value = ""
        if article_col is not None and article_col < len(row_data):
            article_value = row_data[article_col].strip()
            if processor.gcs_article_service.is_url(article_value):
                logger.info(f"Row {row_num}: Article contains URL, fetching from GCS...")
                gcs_data = processor.gcs_article_service.get_article_data(article_value)
                if gcs_data:
                    gcs_title = gcs_data.get("Title", "")
                    gcs_first_para = gcs_data.get("1stp", "")
                    gcs_rest_content = gcs_data.get("Rest of Content", "")
                    gcs_language = gcs_data.get("language", "")

                    updates_to_make = []

                    if not existing_title and gcs_title and title_col is not None:
                        existing_title = gcs_title
                        updates_to_make.append({"row": row_num, "column": config.TITLE_COLUMN, "value": gcs_title})
                        logger.info(f"   Row {row_num}: Title (from GCS): {gcs_title[:50]}...")

                    if not existing_first_para and gcs_first_para and first_para_col is not None:
                        existing_first_para = gcs_first_para
                        updates_to_make.append({"row": row_num, "column": config.FIRST_PARAGRAPH_COLUMN, "value": gcs_first_para})
                        logger.info(f"   Row {row_num}: 1stP (from GCS): {gcs_first_para[:50]}...")

                    if not existing_rest_content and gcs_rest_content and rest_content_col is not None:
                        existing_rest_content = gcs_rest_content
                        updates_to_make.append({"row": row_num, "column": config.REST_CONTENT_COLUMN, "value": gcs_rest_content})
                        logger.info(f"   Row {row_num}: Rest of Content (from GCS): {gcs_rest_content[:50]}...")

                    if gcs_language and language_col is not None:
                        current_language = row_data[language_col].strip() if language_col < len(row_data) else ""
                        if not current_language:
                            updates_to_make.append({"row": row_num, "column": config.LANGUAGE_COLUMN, "value": gcs_language.lower()})
                            logger.info(f"   Row {row_num}: Language (from GCS): {gcs_language}")

                    if updates_to_make:
                        with processor._sheets_lock:
                            processor.sheets_service.batch_update_cells(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                updates_to_make,
                                headers,
                            )
                        logger.info(f"   Row {row_num}: Updated {len(updates_to_make)} columns from GCS")
                else:
                    logger.warning(f"Row {row_num}: Could not fetch article from URL: {article_value[:50]}...")

        # Combine available content
        free_text = ""
        if free_text_col is not None and free_text_col < len(row_data):
            free_text = row_data[free_text_col].strip()

        if free_text:
            article_text = free_text
            logger.info(f"Row {row_num}: Using Free text content ({len(article_text)} chars)")
            if language_col is not None:
                current_language = row_data[language_col].strip() if language_col < len(row_data) else ""
                if not current_language:
                    detected_lang = _detect_language(free_text)
                    if detected_lang:
                        try:
                            processor.sheets_service.update_cell(
                                sheet_id=config.GOOGLE_SHEET_ID,
                                worksheet_name=config.GOOGLE_SHEET_TAB,
                                row=row_num,
                                column_name=config.LANGUAGE_COLUMN,
                                value=detected_lang.lower(),
                                headers=headers,
                            )
                            logger.info(f"   Row {row_num}: Language (from Free text): {detected_lang}")
                        except Exception as e:
                            logger.warning(f"Row {row_num}: Could not update Language column: {e}")
        elif existing_title or existing_first_para or existing_rest_content:
            article_text = f"{existing_title}\n\n{existing_first_para}\n\n{existing_rest_content}".strip()
            logger.info(f"Row {row_num}: Combined article content ({len(article_text)} chars)")
        elif article_value and not processor.gcs_article_service.is_url(article_value):
            article_text = article_value

        vertical = ""
        if vertical_col is not None and vertical_col < len(row_data):
            vertical = row_data[vertical_col].strip()

        subtitle_language = ""
        if language_col is not None and language_col < len(row_data):
            subtitle_language = row_data[language_col].strip().lower()

        manual_vo_text = ""
        if manual_vo_text_col is not None and manual_vo_text_col < len(row_data):
            manual_vo_text = row_data[manual_vo_text_col].strip()

        manual_music_link = ""
        if manual_music_link_col is not None and manual_music_link_col < len(row_data):
            manual_music_link = row_data[manual_music_link_col].strip()

        custom_voice_id = ""
        if voice_id_col is not None and voice_id_col < len(row_data):
            custom_voice_id = row_data[voice_id_col].strip()

        article_related_to_video = True
        if article_related_col is not None and article_related_col < len(row_data):
            article_related_value = row_data[article_related_col].strip().lower()
            if article_related_value == "no":
                article_related_to_video = False
                logger.info(f"Row {row_num}: Article NOT related to video - will create new content while keeping video style")
            elif article_related_value == "yes":
                logger.info(f"Row {row_num}: Article IS related to video - will adapt video for new offer/language")

        if cta_button and cta_text:
            duration_str = "whole video" if cta_duration == "whole_video" else "at the end"
            logger.info(f"Row {row_num}: CTA button enabled: '{cta_text}' ({duration_str})")
        if add_subtitles:
            logger.info(f"Row {row_num}: Subtitles will be added via ZapCap")
        if article_text:
            logger.info(f"Row {row_num}: Article content provided ({len(article_text)} chars)")
        if vertical:
            logger.info(f"Row {row_num}: Vertical: '{vertical}'")
        if subtitle_language:
            logger.info(f"Row {row_num}: Subtitle language: '{subtitle_language}'")
        if manual_vo_text:
            logger.info(f"Row {row_num}: Manual VO text provided ({len(manual_vo_text)} chars)")
        if manual_music_link:
            logger.info(f"Row {row_num}: Manual music link: '{manual_music_link[:50]}...'")
        if custom_voice_id:
            logger.info(f"Row {row_num}: Custom voice ID: '{custom_voice_id}'")

        logger.info(f"\n{'='*60}")
        logger.info(f"Processing row {row_num}: {video_url[:50]}...")
        logger.info(f"{'='*60}")

        try:
            result = processor.process_single_video(
                video_url=video_url,
                row_num=row_num,
                headers=headers,
                manual_instructions=manual_instructions,
                cta_button=cta_button,
                cta_text=cta_text,
                cta_duration=cta_duration,
                add_subtitles=add_subtitles,
                article_text=article_text,
                vertical=vertical,
                subtitle_language=subtitle_language,
                manual_vo_text=manual_vo_text,
                manual_music_link=manual_music_link,
                voice_id=custom_voice_id,
                add_opening_text=add_opening_text,
                opening_text=opening_text,
                animation_model=animation_model,
                article_related_to_video=article_related_to_video,
            )
            return {"row": row_num, "result": result, "success": result.get("success", False)}
        except Exception as e:
            logger.error(f"Error processing row {row_num}: {e}")
            return {"row": row_num, "result": None, "success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Sequential row processing loop
    # ------------------------------------------------------------------
    logger.info(f"Processing {len(data_rows)} rows SEQUENTIALLY (one at a time).")

    for row_idx, row_data in enumerate(data_rows):
        row_num = row_idx + 2
        try:
            logger.info(f"{'='*60}")
            logger.info(f"ROW {row_num}: START")
            logger.info(f"{'='*60}")
            row_result = process_row(row_idx, row_data)
            logger.info(f"ROW {row_num}: COMPLETE")
            if row_result is None:
                continue

            results["processed"] += 1
            if row_result.get("success"):
                results["successful"] += 1
            else:
                results["failed"] += 1
            results["details"].append(row_result.get("result") or row_result)

        except Exception as e:
            logger.error(f"Unexpected error in row {row_num}: {e}")
            results["processed"] += 1
            results["failed"] += 1
            results["details"].append({
                "row": row_num,
                "success": False,
                "error": str(e),
            })

    logger.info(f"\n{'='*60}")
    logger.info(f"Processing complete!")
    logger.info(f"   Processed: {results['processed']}")
    logger.info(f"   Successful: {results['successful']}")
    logger.info(f"   Failed: {results['failed']}")
    logger.info(f"{'='*60}")

    return results


# ---------------------------------------------------------------------------
# Small private helpers (shared across product / UGC / legacy branches)
# ---------------------------------------------------------------------------

def _resolve_animation_model(anim_value: str, row_num: int) -> str:
    """Resolve normalized animation model value to canonical key."""
    if anim_value in ["veo 3.1 fast (vertex ai)", "google veo 3.1", "google 3.1", "google3.1", "veo 3.1", "veo3.1", "veo 3.1 fast", "google fast"]:
        logger.info(f"   Row {row_num}: Using Veo 3.1 Fast (Vertex AI) for video generation")
        return "google31"
    if "3.1" in anim_value and ("google" in anim_value or "veo" in anim_value or "vertex" in anim_value):
        logger.info(f"   Row {row_num}: Using Veo 3.1 Fast (Vertex AI) for video generation")
        return "google31"
    if anim_value in ["kling 2.5 (kie.ai)", "kling 2.5", "kling", "kling v2.5", "kling v2-5"]:
        logger.info(f"   Row {row_num}: Using Kling 2.5 (Kie.ai) for video generation")
        return "kling"
    if anim_value in ["runway gen4 turbo (kie.ai)", "runway gen4", "runway", "runway gen 4"]:
        logger.info(f"   Row {row_num}: Using Runway Gen4 Turbo (Kie.ai) for video generation")
        return "runway"
    if anim_value in ["google 3.0", "google3.0", "veo 3.0", "veo3.0"]:
        return "google"
    if anim_value in ["google", "veo", "veo3", "veo 3"]:
        logger.info(f"   Row {row_num}: Using Google Veo 3 for video generation")
        return "google"
    if anim_value in ["no", "none", "false", "off", "skip"]:
        logger.info(f"   Row {row_num}: Animation disabled — will use Ken Burns from images")
        return "none"
    return "runway"


def _collect_character_urls(
    row_data: List[str],
    character_col: Optional[int],
    character_extra_cols: List[int],
    row_num: int,
) -> List[str]:
    """Collect character image URLs from Character + Character 2/3 columns."""
    character_urls: List[str] = []
    all_char_cols = ([character_col] if character_col is not None else []) + (character_extra_cols or [])
    for col_idx in all_char_cols:
        if col_idx is not None and col_idx < len(row_data):
            cell_val = (row_data[col_idx] or "").strip()
            if cell_val:
                character_urls.extend(_parse_character_urls(cell_val))
    for i, u in enumerate(character_urls):
        if u.startswith("gs://"):
            parts = u.replace("gs://", "").split("/", 1)
            if len(parts) == 2:
                character_urls[i] = f"https://storage.googleapis.com/{parts[0]}/{parts[1]}"
    if character_urls:
        logger.info(f"   Row {row_num}: {len(character_urls)} character image(s) provided")
    elif any((row_data[c] or "").strip() for c in all_char_cols if c is not None and c < len(row_data)):
        logger.warning(f"   Row {row_num}: Character column(s) have value but no valid URL")
    return character_urls


def _get_url_from_col(
    row_data: List[str],
    col_idx: Optional[int],
    row_num: int,
    log_label: str = "",
) -> Optional[str]:
    """Read a URL from a column, returning None if empty or invalid."""
    if col_idx is None or col_idx >= len(row_data):
        return None
    value = row_data[col_idx].strip()
    if value and (value.startswith("http://") or value.startswith("https://")):
        if log_label:
            logger.info(f"   Row {row_num}: {log_label} provided")
        return value
    return None


def _parse_dissolve(row_data: List[str], dissolve_col: Optional[int]) -> Optional[float]:
    """Read dissolve seconds from a column, returning None if empty/invalid."""
    if dissolve_col is None or dissolve_col >= len(row_data):
        return None
    dissolve_raw = row_data[dissolve_col].strip()
    if dissolve_raw:
        try:
            return float(dissolve_raw)
        except ValueError:
            pass
    return None
