"""Google Sheets service for reading and writing spreadsheet data."""

import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class GoogleSheetsService:
    """Service for managing Google Sheets operations."""

    def __init__(self, service_account_file: str):
        """Initialize Google Sheets service.

        Args:
            service_account_file: Path to the service account JSON file.
        """
        self.gc = None
        self._initialize_client(service_account_file)

    def _initialize_client(self, service_account_file: str) -> None:
        """Initialize the Google Sheets client.

        Args:
            service_account_file: Path to the service account JSON file.
        """
        try:
            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]

            credentials = Credentials.from_service_account_file(
                service_account_file,
                scopes=scopes
            )

            self.gc = gspread.authorize(credentials)
            logger.info("Google Sheets client initialized successfully")

        except Exception as e:
            logger.warning(f"Failed to initialize Google Sheets client: {e}")
            raise

    def get_worksheet_data(
        self,
        sheet_id: str,
        worksheet_name: str,
        max_retries: int = 3,
        base_delay: float = 2.0
    ) -> Tuple[List[str], List[List[str]]]:
        """Get all data from a worksheet with retry logic.

        Args:
            sheet_id: Google Sheet ID.
            worksheet_name: Name of the worksheet tab.
            max_retries: Maximum retry attempts for API errors.
            base_delay: Base delay for exponential backoff.

        Returns:
            Tuple of (headers, data_rows).
        """
        for attempt in range(max_retries + 1):
            try:
                spreadsheet = self.gc.open_by_key(sheet_id)
                # Try to get worksheet by name, fallback to first sheet
                try:
                    worksheet = spreadsheet.worksheet(worksheet_name)
                except Exception:
                    logger.warning(f"Worksheet '{worksheet_name}' not found, using first sheet")
                    worksheet = spreadsheet.get_worksheet(0)
                    if worksheet:
                        logger.info(f"Using sheet: {worksheet.title}")
                all_values = worksheet.get_all_values()

                if not all_values:
                    return [], []

                headers = all_values[0]
                data_rows = all_values[1:] if len(all_values) > 1 else []

                logger.info(f"Retrieved {len(data_rows)} rows from {worksheet_name}")
                return headers, data_rows

            except Exception as e:
                error_str = str(e).lower()

                # Check if it's a retryable error
                is_retryable = any(err in error_str for err in [
                    'rate limit', 'quota', '429', '503', '500',
                    'service unavailable', 'internal error',
                    'timeout', 'connection', 'temporarily unavailable',
                    'apiexception', 'exceeded', 'resource exhausted'
                ])

                if is_retryable and attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + (time.time() % 1)
                    logger.warning(
                        f"Google Sheets read error (attempt {attempt + 1}/{max_retries + 1}): {e}"
                        f"\n   Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Error getting worksheet data: {e}")
                    raise

        return [], []  # Should never reach here

    def get_column_index(self, headers: List[str], column_name: str) -> int:
        """Get column index by name.

        Args:
            headers: List of header names.
            column_name: Name of the column to find.

        Returns:
            Column index (0-based).

        Raises:
            ValueError: If column not found.
        """
        try:
            return headers.index(column_name)
        except ValueError:
            raise ValueError(f"Column '{column_name}' not found in headers: {headers[:10]}...")

    def get_row(
        self,
        sheet_id: str,
        worksheet_name: str,
        row_num: int,
        max_retries: int = 3,
        base_delay: float = 2.0
    ) -> Optional[List[str]]:
        """Get a single row from the worksheet.

        Args:
            sheet_id: Google Sheet ID.
            worksheet_name: Name of the worksheet tab.
            row_num: Row number (1-indexed, where 1 is header).
            max_retries: Maximum retry attempts for API errors.
            base_delay: Base delay for exponential backoff.

        Returns:
            List of cell values for the row, or None if failed.
        """
        for attempt in range(max_retries + 1):
            try:
                spreadsheet = self.gc.open_by_key(sheet_id)
                try:
                    worksheet = spreadsheet.worksheet(worksheet_name)
                except Exception:
                    worksheet = spreadsheet.get_worksheet(0)

                row_values = worksheet.row_values(row_num)
                return row_values

            except Exception as e:
                error_str = str(e).lower()

                is_retryable = any(err in error_str for err in [
                    'rate limit', 'quota', '429', '503', '500',
                    'service unavailable', 'internal error',
                    'timeout', 'connection', 'resource exhausted'
                ])

                if is_retryable and attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + (time.time() % 1)
                    logger.warning(f"Get row error (attempt {attempt + 1}): {e}, retrying in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"Error getting row {row_num}: {e}")
                    return None

        return None

    def update_cell(
        self,
        sheet_id: str,
        worksheet_name: str,
        row: int,
        column_name: str,
        value: str,
        headers: List[str],
        max_retries: int = 6,
        base_delay: float = 10.0
    ) -> None:
        """Update a single cell in the worksheet with retry logic.

        Args:
            sheet_id: Google Sheet ID.
            worksheet_name: Name of the worksheet tab.
            row: Row number (1-based).
            column_name: Name of the column.
            value: Value to set.
            headers: List of header names.
            max_retries: Maximum retry attempts for API errors.
            base_delay: Base delay for exponential backoff.
        """
        if row is None or not isinstance(row, int) or row < 1:
            logger.debug("update_cell skipped: no valid sheet row (row=%r)", row)
            return
        if not headers:
            logger.debug("update_cell skipped: empty headers")
            return

        col_idx = self.get_column_index(headers, column_name) + 1  # gspread uses 1-based

        for attempt in range(max_retries + 1):
            try:
                spreadsheet = self.gc.open_by_key(sheet_id)
                worksheet = spreadsheet.worksheet(worksheet_name)
                worksheet.update_cell(row, col_idx, value)
                logger.info(f"Updated cell ({row}, {column_name})")
                return

            except Exception as e:
                error_str = str(e).lower()

                # Check if it's a retryable error
                is_retryable = any(err in error_str for err in [
                    'rate limit', 'quota', '429', '503', '500',
                    'service unavailable', 'internal error',
                    'timeout', 'connection', 'temporarily unavailable',
                    'apiexception', 'exceeded', 'resource exhausted'
                ])

                # Check if it's specifically a rate limit error (needs longer delay)
                is_rate_limit = any(err in error_str for err in [
                    'rate limit', 'quota', '429', 'exceeded', 'resource exhausted',
                    'read requests', 'write requests'
                ])

                if is_retryable and attempt < max_retries:
                    if is_rate_limit:
                        # For rate limits: wait 65+ seconds to let quota reset (60 req/min limit)
                        delay = 65 + (attempt * 10) + (time.time() % 5)
                        logger.warning(
                            f"Google Sheets RATE LIMIT (attempt {attempt + 1}/{max_retries + 1}): "
                            f"Quota exceeded. Waiting {delay:.0f}s for quota to reset..."
                        )
                    else:
                        # Standard exponential backoff for other errors
                        delay = base_delay * (2 ** attempt) + (time.time() % 1)
                        logger.warning(
                            f"Google Sheets API error (attempt {attempt + 1}/{max_retries + 1}): {e}"
                            f"\n   Retrying in {delay:.1f}s..."
                        )
                    time.sleep(delay)
                else:
                    logger.error(f"Error updating cell ({row}, {column_name}): {e}")
                    raise

    def batch_update_cells(
        self,
        sheet_id: str,
        worksheet_name: str,
        updates: List[Dict[str, Any]],
        headers: List[str],
        max_retries: int = 6,
        base_delay: float = 10.0
    ) -> None:
        """Batch update multiple cells with retry logic.

        Args:
            sheet_id: Google Sheet ID.
            worksheet_name: Name of the worksheet tab.
            updates: List of dicts with 'row', 'column', 'value' keys.
            headers: List of header names.
            max_retries: Maximum retry attempts for API errors.
            base_delay: Base delay for exponential backoff.
        """
        if not updates:
            return
        updates = [
            u for u in updates
            if u.get("row") is not None
            and isinstance(u.get("row"), int)
            and u["row"] >= 1
        ]
        if not updates:
            return

        for attempt in range(max_retries + 1):
            try:
                spreadsheet = self.gc.open_by_key(sheet_id)
                worksheet = spreadsheet.worksheet(worksheet_name)

                batch_data = []
                for update in updates:
                    row_num = update['row']
                    column = update['column']
                    value = update['value']

                    col_idx = self.get_column_index(headers, column) + 1
                    col_letter = self._column_index_to_letter(col_idx)
                    cell_address = f"{col_letter}{row_num}"

                    batch_data.append({
                        'range': cell_address,
                        'values': [[str(value)]]
                    })

                if batch_data:
                    worksheet.batch_update(batch_data)
                    logger.info(f"Batch updated {len(batch_data)} cells")
                return

            except Exception as e:
                error_str = str(e).lower()

                # Check if it's a retryable error
                is_retryable = any(err in error_str for err in [
                    'rate limit', 'quota', '429', '503', '500',
                    'service unavailable', 'internal error',
                    'timeout', 'connection', 'temporarily unavailable',
                    'apiexception', 'exceeded', 'resource exhausted'
                ])

                # Check if it's specifically a rate limit error (needs longer delay)
                is_rate_limit = any(err in error_str for err in [
                    'rate limit', 'quota', '429', 'exceeded', 'resource exhausted',
                    'read requests', 'write requests'
                ])

                if is_retryable and attempt < max_retries:
                    if is_rate_limit:
                        # For rate limits: wait 65+ seconds to let quota reset (60 req/min limit)
                        delay = 65 + (attempt * 10) + (time.time() % 5)
                        logger.warning(
                            f"Google Sheets RATE LIMIT (attempt {attempt + 1}/{max_retries + 1}): "
                            f"Quota exceeded. Waiting {delay:.0f}s for quota to reset..."
                        )
                    else:
                        # Standard exponential backoff for other errors
                        delay = base_delay * (2 ** attempt) + (time.time() % 1)
                        logger.warning(
                            f"Google Sheets batch update error (attempt {attempt + 1}/{max_retries + 1}): {e}"
                            f"\n   Retrying in {delay:.1f}s..."
                        )
                    time.sleep(delay)
                else:
                    logger.error(f"Error in batch update: {e}")
                    raise

    def _column_index_to_letter(self, col_idx: int) -> str:
        """Convert column index to letter (1=A, 2=B, etc.)."""
        result = ""
        while col_idx > 0:
            col_idx -= 1
            result = chr(65 + col_idx % 26) + result
            col_idx //= 26
        return result


class NoOpSheetsService:
    """No-op Sheets service for API/UI mode when no credentials are present.
    Implements the same interface as GoogleSheetsService but does nothing.
    Allows the pipeline to run without Google Sheets (e.g. Studio UI)."""

    def get_worksheet_data(
        self,
        sheet_id: str,
        worksheet_name: str,
        max_retries: int = 3,
        base_delay: float = 2.0
    ) -> Tuple[List[str], List[List[str]]]:
        return [], []

    def get_column_index(self, headers: List[str], column_name: str) -> int:
        return 0

    def get_row(
        self,
        sheet_id: str,
        worksheet_name: str,
        row_num: int,
        max_retries: int = 3,
        base_delay: float = 2.0
    ) -> Optional[List[str]]:
        return None

    def update_cell(
        self,
        sheet_id: str,
        worksheet_name: str,
        row: int,
        column_name: str,
        value: str,
        headers: List[str],
        max_retries: int = 6,
        base_delay: float = 10.0
    ) -> None:
        pass

    def batch_update_cells(
        self,
        sheet_id: str,
        worksheet_name: str,
        updates: List[Dict[str, Any]],
        headers: List[str],
        max_retries: int = 6,
        base_delay: float = 10.0
    ) -> None:
        pass
