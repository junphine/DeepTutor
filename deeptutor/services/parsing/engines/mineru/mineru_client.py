"""PDF to Markdown converter using MinerU service."""

from __future__ import annotations

import argparse
import logging
import os,sys
import httpx,json
import zipfile
import time
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Any
from collections.abc import Callable
from pypdf import PdfReader, PdfWriter
from deeptutor.services.keypool import KeyPool

from .checkpoints import SliceCheckpoint, job_directory
from .config import MinerUConfig, MinerUError
from .formats import MINERU_PDF_FORMATS, MINERU_SUPPORTED_FORMATS

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 600.0

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class MinerUClient:
    def __init__(
        self,
        base_url: str = None,
        api_token: str = None,
        is_ocr: bool = True,
        language: str = None,
        enable_table: bool = True,
        enable_formula: bool = True,
        model_version: str | None = None,
        page_ranges: str = "1--1", # 默认第一页到最后一页
        poll_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self.base_url = base_url or os.getenv("MINERU_BASE_URL", "")
        self.api_token = api_token or os.getenv("MINERU_API_TOKEN", "")
        self.is_ocr = is_ocr
        self.language = language or os.getenv("MINERU_LANGUAGE", "ch")
        self.enable_table = enable_table
        self.enable_formula = enable_formula
        self.model_version = model_version or os.getenv("MINERU_MODEL_VERSION", "pipeline")
        self.page_ranges = page_ranges
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.headers = {"Authorization": f"Bearer {self.api_token}"}
        if not self.base_url:
            raise ValueError("MINERU_BASE_URL is not set")
        if not self.api_token:
            raise ValueError("MINERU_API_TOKEN is not set")

    def export_batch_markdown_bundle(
        self,
        file_paths: list[Path],
        output_dir: Path,
    ):
        logger.info(f"[MinerU] Starting export_batch_markdown_bundle for {len(file_paths)} files")
        output_dir.mkdir(parents=True, exist_ok=True)
        batch_id, file_urls = self.submit_tasks(file_paths)
        logger.info(f"[MinerU] Task submitted, batch_id={batch_id}")
        all_dones = self._poll_until_all_done(batch_id, file_urls)
        logger.info(f"[MinerU] Polling completed for batch_id={batch_id}")

        zip_contents = self._download_zip(all_dones)
        total_bytes = sum(len(v) for v in zip_contents.values())
        logger.info(f"[MinerU] Downloaded zip, size={total_bytes} bytes")
        self._extract_from_zip(zip_contents, output_dir)
        logger.info(f"[MinerU] Extracted to {output_dir}")
      
    def submit_tasks(self, file_paths: list[Path], timeout:float=60.0) -> tuple[str, list[str]]:
        logger.info(f"[MinerU] Submitting {len(file_paths)} files to {self.base_url}")
        payload = {
            "files": [
                {"name": fp.name, "is_ocr": self.is_ocr, "page_ranges": self.page_ranges}
                for fp in file_paths
            ],
            "enable_table": self.enable_table,
            "enable_formula": self.enable_formula,
            "language": self.language,
            "model_version": self.model_version,
        }
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            resp = client.post("/file-urls/batch", headers=self.headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data["code"] != 0:
            raise ValueError(f"MinerU create task failed: {data.get('msg', 'unknown error')}")

        task_data = data.get("data", {})
        batch_id = task_data.get("batch_id")
        file_urls = task_data.get("file_urls")

        with httpx.Client(timeout=timeout) as client:
            for fp, url in zip(file_paths, file_urls):
                with fp.open("rb") as f:
                    resp = client.put(url, content=f)
                if resp.status_code not in (200, 201):
                    logger.error(f"MinerU upload failed for {fp.name} with status {resp.status_code}")
                    raise ValueError(f"MinerU upload failed for {fp.name} with status {resp.status_code}")
        return batch_id, file_urls

    def _poll_until_all_done(self, batch_id: str, file_urls:list[str], timeout:float=6000.0) -> dict[str, str]:
        logger.info(f"[MinerU] Starting polling for batch_id={batch_id}")
        start_time = time.time()
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            all_dones = dict()
            while len(all_dones) < len(file_urls):
                elapsed = time.time() - start_time
                if elapsed > self.poll_timeout_seconds:
                    raise TimeoutError(f"Poll timeout after {self.poll_timeout_seconds}s for batch_id={batch_id}")
                resp = client.get(f"/extract-results/batch/{batch_id}", headers=self.headers)
                resp.raise_for_status()
                poll_data = resp.json()
                if poll_data.get("code") != 0:
                    raise ValueError(f"MinerU poll failed: {poll_data.get('msg', 'unknown error')}")
                extract_result = poll_data.get("data", {}).get("extract_result")
                for item in extract_result:
                    file_name = Path(item['file_name']).stem
                    if item['state'] == 'done':
                        all_dones[file_name] = item["full_zip_url"]
                        logger.debug(f"[MinerU] File {file_name} done")
                    elif item['state'] == 'failed':
                        all_dones[file_name] = "failed"
                        logger.error(f"MinerU parse failed for {file_name}: {item.get('err_msg')}")
                if len(all_dones) < len(file_urls):
                    logger.debug(f"[MinerU] Waiting for {len(file_urls) - len(all_dones)} files...")
                    time.sleep(self.poll_interval_seconds)
        logger.info(f"[MinerU] All {len(all_dones)} files completed")
        return all_dones

      
    def _download_zip(self, all_dones: dict[str,str], timeout:float=60.0) -> dict[str,bytes]:
        logger.info(f"[MinerU] Downloading {len(all_dones)} zip files")
        zip_contents = {}
        with httpx.Client(timeout=timeout) as client:
            for file_name, zip_url in all_dones.items():
                logger.debug(f"[MinerU] Downloading {file_name}")
                zip_resp = client.get(zip_url)
                zip_resp.raise_for_status()
                zip_contents[file_name] = zip_resp.content
                logger.debug(f"[MinerU] Downloaded {file_name}, size={len(zip_resp.content)} bytes")
        return zip_contents

    def _extract_from_zip(self, zip_contents: dict[str,bytes], extract_dir: Path) -> str:
        for file_name, zip_content in zip_contents.items():
            file_output_dir = extract_dir / file_name
            with zipfile.ZipFile(BytesIO(zip_content)) as zf:
                zf.extractall(file_output_dir)
        logger.info(f"[MinerU] Extracted {len(zip_contents)} files to {extract_dir}")


class MinerUClientLocal:
    def __init__(
        self,
        base_url: str = None,
        api_token: str = None,
        lang_list: list[str] = None,
        backend: str = "pipeline",
        parse_method: str = "auto",
        formula_enable: bool = True,
        table_enable: bool = True,
        return_md: bool = True,
        return_images: bool = True,
        response_format_zip: bool = True,
        return_middle_json: bool = False,
        return_content_list: bool = True,
        return_model_output: bool = True,
        start_page_id: int = 0,
        end_page_id: int | None = None,
        poll_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self.base_url = base_url or os.getenv("MINERU_BASE_URL", "http://127.0.0.1:8000")
        self.api_token = api_token or os.getenv("MINERU_API_TOKEN", "")
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._form_data = {
            "lang_list": lang_list if lang_list else ["ch","en"],
            "backend": backend,
            "parse_method": parse_method,
            "formula_enable": formula_enable,
            "table_enable": table_enable,
            "return_md": return_md,
            "response_format_zip": response_format_zip,
            "return_images": return_images,
            "return_middle_json": return_middle_json,
            "return_content_list": return_content_list,
            "return_model_output": return_model_output,
            "start_page_id": start_page_id,
            "end_page_id": end_page_id,            
        }
    
    def export_batch_markdown_bundle(
        self,
        file_paths: list[Path],
        output_dir: Path,
        timeout: float | None = None
    ) -> dict[str, dict]:
        logger.info(f"[MinerU] Starting export_batch_markdown_bundle for {len(file_paths)} files")
        output_dir.mkdir(parents=True, exist_ok=True)
        task_id = self.submit_tasks(file_paths,timeout=timeout)
        logger.info(f"[MinerU] Task submitted, task_id={task_id}")
        result = self._poll_until_all_done(task_id)
        logger.info(f"[MinerU] Polling completed for task_id={task_id}")

        zip_content = self._download_zip(task_id)
        logger.info(f"[MinerU] Downloaded zip, size={len(zip_content)} bytes")
        self._extract_from_zip(zip_content, output_dir)
        logger.info(f"[MinerU] Extracted to {output_dir}")
        return result
    
    def dryrun_export_batch_markdown_bundle(
        self,
        file_paths: list[Path],
        output_dir: Path,
    ) -> dict[str, dict]:
        logger.info(f"[MinerU] Starting export_batch_markdown_bundle for {len(file_paths)} files")
        output_dir.mkdir(parents=True, exist_ok=True)
        task_id = 'self.submit_tasks(file_paths)'
        logger.info(f"[MinerU] Task submitted, task_id={task_id}")
        result = dict(task_id=task_id,file_paths=str(file_paths))
        logger.info(f"[MinerU] Polling completed for task_id={task_id}")
        
        logger.info(f"[MinerU] Extracted to {output_dir}")
        return result
        
    def submit_tasks(self, file_paths: list[Path], timeout:float=600.0) -> str:
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            files = [
                ("files", (fp.name, fp.open("rb"), "application/pdf"))
                for fp in file_paths]
            resp = client.post("/tasks", data=self._form_data, files=files)
            resp.raise_for_status()
            task_id = resp.json().get("task_id")
        return task_id

    def _poll_until_all_done(self, task_id: str, timeout:float=6000.0) -> dict[str, dict]:
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            resp = client.get(f"tasks/{task_id}")
            json = resp.json()
            while json['status']!='completed':
                if json['status'] == 'failed':
                    logger.error(f"[MinerU] Task failed, task_id={task_id}, error={json['error']}")
                    break
                time.sleep(self.poll_interval_seconds)
                resp = client.get(f"tasks/{task_id}")
                json = resp.json()
            resp.raise_for_status()
            return json
        return None

    def _download_zip(self, task_id: str, timeout:float=60.0) -> bytes:
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            resp = client.get(
                f"/tasks/{task_id}/result",
                params={"response_format_zip": "true"},
            )
            resp.raise_for_status()
            return resp.content

    def _extract_from_zip(self, zip_content: bytes, extract_dir: Path) -> dict:
        with zipfile.ZipFile(BytesIO(zip_content)) as zf:
            zf.extractall(extract_dir)


def verify_service(config: MinerUConfig):
    """Best-effort connectivity / token check for the Settings → MinerU "Test"
    button. Requests an upload slot (which does not consume parsing quota and
    is never followed by an upload, so it simply expires) and validates the
    business code. Raises :class:`MinerUError` with a user-facing message on
    any failure."""
    if not config.api_keys:
        raise MinerUError("No API token configured.")
    base_url = config.api_base_url.rstrip("/")
    key_pool = KeyPool(config.api_keys)
    lang_list = []
    if config.api_language:
        lang_list.append(config.api_language)

    client = MinerUClientLocal(
        base_url=base_url,
        api_token=config.api_token,
        lang_list=lang_list,
        formula_enable=config.enable_formula,
        table_enable=config.enable_table
    )

    return client.submit_tasks([Path("connectivity-check.pdf")])


def parse_remote(
        source_path: Path,
        output_base: Path,
        config: MinerUConfig,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        on_progress: Callable[[str], None] | None = None,
) -> Path:
    """Parse ``source_path`` via the MinerU cloud API; return the working dir.

    The working dir sits under ``output_base`` (named after the input stem) and
    holds the unzipped MinerU artifacts. ``on_progress`` (if given) receives a
    short status line whenever the polled task state / page count changes.
    Raises :class:`MinerUError` on any misconfiguration, API error, timeout,
    or extraction failure.
    """
    if not config.api_keys:
        raise MinerUError(
            "MinerU cloud mode is selected but no API token is configured. "
            "Add a token in Settings → MinerU, or switch to local mode."
        )
    source_path = Path(source_path)
    if not source_path.is_file():
        raise MinerUError(f"Input file not found: {source_path}")
    if source_path.suffix.lower() not in MINERU_SUPPORTED_FORMATS:
        raise MinerUError(f"Unsupported MinerU cloud input format: {source_path.suffix or 'none'}")

    base_url = config.api_base_url.rstrip("/")
    key_pool = KeyPool(config.api_keys)

    def report(message: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(message)
        except Exception:
            logger.debug("on_progress callback failed", exc_info=True)

    base_url = config.api_base_url.rstrip("/")
    key_pool = KeyPool(config.api_keys)
    lang_list = []
    if config.api_language:
        lang_list.append(config.api_language)

    page_count = None

    size_mb = source_path.stat().st_size / (1024 * 1024)
    report(f"MinerU cloud: uploading {source_path.name} ({size_mb:.1f} MB)")

    if source_path.suffix.lower() in MINERU_PDF_FORMATS:
        page_count = _pdf_page_count(source_path)
        if page_count is not None and page_count >= config.max_pages_per_part:
            total = page_count // config.max_pages_per_part
            report(
                f"MinerU cloud: {source_path.name} has {page_count} pages, exceeding the "
                f"{config.max_pages_per_part}-page per-file limit; auto-splitting into "
                f"{total} part(s)"
            )
            page_count = config.max_pages_per_part


    client = MinerUClientLocal(
        base_url=base_url,
        api_token=config.api_token,
        lang_list=lang_list,
        formula_enable=config.enable_formula,
        table_enable=config.enable_table,
        start_page_id=0,
        end_page_id =page_count
    )

    report(f"MinerU cloud: requesting upload slot for {source_path.name}")
    archive_bytes = client.export_batch_markdown_bundle(
        [source_path],
        output_base,
        timeout=timeout,
    )

    report("MinerU cloud: extracting archive")
    working_dir = output_base / source_path.stem
    logger.info("MinerU cloud parse complete: %s → %s", source_path.name, working_dir)
    return working_dir

def _pdf_page_count(source_path: Path) -> int | None:
    """Number of pages in a PDF, or ``None`` when pypdf cannot read it (e.g. a
    truncated/encrypted file). ``None`` keeps the legacy single-file behaviour —
    an unreadable PDF goes to MinerU as-is and fails there if it is truly broken."""
    try:
        with PdfReader(str(source_path)) as reader:
            return len(reader.pages)
    except Exception:
        logger.warning("Could not count pages of %s; skipping auto-slicing", source_path.name)
        return None





