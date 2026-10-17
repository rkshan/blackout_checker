"""FastAPI job API for video QC analysis."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from analysis import (
    AnalysisError,
    VIDEO_EXTENSIONS,
    aggregate_issues,
    analyze_xml,
    blackdetect_command,
    parse_blackdetect_log,
    probe_video,
)

load_dotenv(Path(__file__).parent / ".env")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
MAX_ANALYSIS_SECONDS = int(os.getenv("MAX_ANALYSIS_SECONDS", "7200"))
TEMP_ROOT = Path(tempfile.gettempdir()) / "video-qc-analyzer"
TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)

app = FastAPI(title="Video QC Analyzer API", version="1.0.0")
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

jobs: dict[str, dict[str, Any]] = {}
workers: dict[str, asyncio.Task[None]] = {}


def _build_summary(issues: list[dict[str, Any]], duration: float | None) -> dict[str, Any]:
    black_types = {"BLACKOUT", "BLACK_FRAME"}
    gap_types = {"EMPTY_TIMELINE_GAP", "TIMELINE_DISCONTINUITY"}
    discontinuity_types = {"TIMELINE_DISCONTINUITY", "MISSING_SEGMENT"}

    def includes(issue: dict[str, Any], types: set[str]) -> bool:
        return issue["type"] in types or bool(set(issue.get("corroboratingTypes", [])) & types)

    return {
        "totalVideoDuration": duration,
        "totalIssues": len(issues),
        "blackoutCount": sum(issue["type"] == "BLACKOUT" or "BLACKOUT" in issue.get("corroboratingTypes", []) for issue in issues),
        "blackFrameCount": sum(issue["type"] == "BLACK_FRAME" or "BLACK_FRAME" in issue.get("corroboratingTypes", []) for issue in issues),
        "timelineGapCount": sum(includes(issue, gap_types) for issue in issues),
        "missingOrDiscontinuousCount": sum(includes(issue, discontinuity_types) for issue in issues),
        "confirmedBlackIntervalDuration": sum(issue["duration"] for issue in issues if includes(issue, black_types)),
        "identifiedTimelineGapDuration": sum(issue["duration"] for issue in issues if includes(issue, gap_types)),
    }


async def _detect_black_intervals(video_path: Path, settings: dict[str, Any]) -> list[dict[str, Any]]:
    threshold = settings["pixelThreshold"] if settings["nearBlackEnabled"] else min(settings["pixelThreshold"], 0.01)
    try:
        process = await asyncio.create_subprocess_exec(
            *blackdetect_command(video_path, threshold),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AnalysisError("FFmpeg is required but was not found. Install FFmpeg and ensure ffmpeg is on PATH.") from exc

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=MAX_ANALYSIS_SECONDS)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise AnalysisError("Video analysis exceeded the configured processing time limit.") from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    log = stderr.decode("utf-8", errors="replace")
    intervals = parse_blackdetect_log(
        log,
        threshold,
        settings["minBlackoutDuration"],
        settings["nearBlackEnabled"],
    )
    if process.returncode and not intervals:
        detail = log.strip()[-1200:]
        raise AnalysisError(f"FFmpeg failed to analyze the video: {detail or 'decoder error'}")
    return intervals


async def _save_upload(upload: UploadFile, destination: Path, limit: int) -> int:
    total = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"Upload exceeds the {limit // (1024 * 1024)} MB configured limit.")
            output.write(chunk)
    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'Uploaded file'} is empty.")
    return total


async def _run_analysis(job_id: str, video_path: Path | None, xml_path: Path, job_dir: Path, settings: dict[str, Any]) -> None:
    job = jobs[job_id]
    try:
        job["status"] = "processing"
        metadata = None
        video_issues: list[dict[str, Any]] = []
        if video_path:
            job["stage"] = "Reading video metadata"
            metadata = await asyncio.to_thread(probe_video, video_path)
            job["stage"] = "Scanning video frames"
            video_issues = await _detect_black_intervals(video_path, settings)

        job["stage"] = "Analyzing XML timeline"
        xml_bytes = await asyncio.to_thread(xml_path.read_bytes)
        xml_result = await asyncio.to_thread(
            analyze_xml,
            xml_bytes,
            metadata["duration"] if metadata else None,
        )
        xml_issues = xml_result["issues"]

        issues = aggregate_issues(video_issues, xml_issues)
        job["result"] = {
            "video": {"filename": job["videoFilename"], **metadata} if metadata else None,
            "xml": {"filename": job["xmlFilename"], **xml_result},
            "settings": settings,
            "analysisDate": datetime.now(timezone.utc).isoformat(),
            "issues": issues,
            "summary": _build_summary(issues, metadata["duration"] if metadata else None),
            "warnings": ([xml_result["warning"]] if xml_result["warning"] else []),
            "limitations": [
                "XML analysis recognizes common timestamp and source fields; vendor-specific schemas may need field mapping.",
                *(
                    [
                        "Black interval detection is pixel-based and may flag intentional dark scenes.",
                        "Frame boundary precision depends on source timestamps, frame rate, and decoder behavior.",
                    ]
                    if metadata
                    else []
                ),
            ],
        }
        job["status"] = "completed"
        job["stage"] = "Analysis complete"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["stage"] = "Analysis cancelled"
        raise
    except AnalysisError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["stage"] = "Analysis failed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"Unexpected analysis error: {exc}"
        job["stage"] = "Analysis failed"
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/analyze", status_code=202)
async def create_analysis(
    xml: UploadFile = File(...),
    video: UploadFile | None = File(default=None),
    pixel_threshold: float = Form(default=0.10),
    min_blackout_duration: float = Form(default=0.5),
    near_black_enabled: bool = Form(default=True),
) -> dict[str, str]:
    xml_name = Path(xml.filename or "").name
    if Path(xml_name).suffix.lower() != ".xml":
        raise HTTPException(status_code=415, detail="The timeline metadata file must use the .xml extension.")
    video_name = Path(video.filename or "").name if video else None
    if video and Path(video_name or "").suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported video extension. Upload MP4, MOV, MXF, MKV, AVI, WebM, or MPEG.")
    if not 0.001 <= pixel_threshold <= 0.5:
        raise HTTPException(status_code=422, detail="Pixel threshold must be between 0.001 and 0.5.")
    if not 0 <= min_blackout_duration <= 60:
        raise HTTPException(status_code=422, detail="Minimum blackout duration must be between 0 and 60 seconds.")
    job_id = str(uuid.uuid4())
    job_dir = TEMP_ROOT / job_id
    job_dir.mkdir(mode=0o700)
    video_path = job_dir / f"video{Path(video_name).suffix.lower()}" if video and video_name else None
    xml_path = job_dir / "timeline.xml"
    try:
        await _save_upload(xml, xml_path, min(MAX_UPLOAD_BYTES, 50 * 1024 * 1024))
        if video and video_path:
            await _save_upload(video, video_path, MAX_UPLOAD_BYTES)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await xml.close()
        if video:
            await video.close()

    settings = {
        "pixelThreshold": pixel_threshold,
        "minBlackoutDuration": min_blackout_duration,
        "nearBlackEnabled": near_black_enabled,
    }
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "Waiting to start",
        "videoFilename": video_name,
        "xmlFilename": xml_name,
        "result": None,
        "error": None,
    }
    workers[job_id] = asyncio.create_task(_run_analysis(job_id, video_path, xml_path, job_dir, settings))
    return {"jobId": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    return job


@app.delete("/api/jobs/{job_id}", status_code=202)
async def cancel_job(job_id: str) -> dict[str, str]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    if job["status"] in {"queued", "processing"}:
        worker = workers.get(job_id)
        if worker:
            worker.cancel()
        job["status"] = "cancelled"
        job["stage"] = "Analysis cancelled"
    return {"status": job["status"]}
