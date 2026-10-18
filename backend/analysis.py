"""Video and schema-adaptive XML timeline analysis."""

from __future__ import annotations

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import fromstring

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".webm", ".mpeg", ".mpg"}
TIME_KEYS = {"start", "starttime", "in", "position", "begin", "offset"}
END_KEYS = {"end", "endtime", "out", "finish"}
DURATION_KEYS = {"duration", "length", "dur"}
SOURCE_KEYS = {"src", "source", "media", "filename", "file", "href", "path"}


class AnalysisError(ValueError):
    """Raised when input media cannot be analyzed."""


def parse_time(value: Any) -> float | None:
    """Parse seconds or common clock/timecode strings into seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) and number >= 0 else None
    except ValueError:
        pass

    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        if minutes >= 60 or seconds >= 60:
            return None
        return hours * 3600 + minutes * 60 + seconds

    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|sec|seconds?)", text, re.IGNORECASE)
    if match:
        value_seconds = float(match.group(1))
        return value_seconds / 1000 if match.group(2).lower() == "ms" else value_seconds
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _local_name(key).lower())


def analyze_xml(xml_bytes: bytes, video_duration: float | None = None) -> dict[str, Any]:
    """Extract supported timeline entries and report explicit inferred issues only."""
    try:
        root = fromstring(xml_bytes)
    except Exception as exc:
        raise AnalysisError(f"Could not parse XML: {exc}") from exc

    elements = list(root.iter())
    segments: list[dict[str, Any]] = []
    element_names: set[str] = set()
    explicit_gaps: list[dict[str, Any]] = []
    explicit_missing: list[dict[str, Any]] = []
    for element in elements:
        name = _local_name(element.tag)
        element_names.add(name)
        attrs = {_normalized_key(k): v for k, v in element.attrib.items()}
        values = dict(attrs)
        for child in list(element):
            child_name = _normalized_key(child.tag)
            if child.text and child.text.strip():
                values.setdefault(child_name, child.text.strip())

        start = next((parse_time(values[k]) for k in TIME_KEYS if k in values and parse_time(values[k]) is not None), None)
        end = next((parse_time(values[k]) for k in END_KEYS if k in values and parse_time(values[k]) is not None), None)
        duration = next((parse_time(values[k]) for k in DURATION_KEYS if k in values and parse_time(values[k]) is not None), None)
        if end is None and start is not None and duration is not None:
            end = start + duration
        source = next((str(values[k]).strip() for k in SOURCE_KEYS if values.get(k)), None)
        segment_element = any(token in name.lower() for token in ("clip", "segment", "entry", "trackitem", "shot", "event"))
        marker_element = any(token in name.lower() for token in ("gap", "empty", "blank", "missing", "offline"))
        if start is not None and end is not None and end >= start and (source or segment_element) and not marker_element:
            segments.append({
                "startTime": start,
                "endTime": end,
                "duration": end - start,
                "source": source,
                "element": name,
            })

        lowered = name.lower()
        marker = " ".join(str(v) for v in values.values()).lower()
        if any(word in lowered for word in ("gap", "empty", "blank")) and start is not None and end is not None and end > start:
            explicit_gaps.append({"startTime": start, "endTime": end, "duration": end - start})
        if any(word in lowered for word in ("missing", "offline", "unavailable")) and start is not None and end is not None and end > start:
            explicit_missing.append({"startTime": start, "endTime": end, "duration": end - start})
        if values.get("missing", "").lower() in {"true", "1", "yes"} and start is not None and end is not None and end > start:
            explicit_missing.append({"startTime": start, "endTime": end, "duration": end - start})
        if any(term in marker for term in ("offline", "missing media", "media offline")) and start is not None and end is not None and end > start:
            explicit_missing.append({"startTime": start, "endTime": end, "duration": end - start})

    segments.sort(key=lambda segment: (segment["startTime"], segment["endTime"]))
    issues: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        if index == 0:
            continue
        previous = segments[index - 1]
        delta = segment["startTime"] - previous["endTime"]
        if delta > 0.001:
            represented_by_explicit_gap = any(
                abs(gap["startTime"] - previous["endTime"]) <= 0.001
                and abs(gap["endTime"] - segment["startTime"]) <= 0.001
                for gap in explicit_gaps
            )
            if represented_by_explicit_gap:
                continue
            issues.append({
                "type": "TIMELINE_DISCONTINUITY",
                "startTime": previous["endTime"],
                "endTime": segment["startTime"],
                "duration": delta,
                "severity": "MEDIUM",
                "source": ["XML"],
                "description": "A gap between consecutive timestamped XML segments was inferred.",
            })
        elif delta < -0.001:
            issues.append({
                "type": "TIMELINE_OVERLAP",
                "startTime": segment["startTime"],
                "endTime": previous["endTime"],
                "duration": -delta,
                "severity": "MEDIUM",
                "source": ["XML"],
                "description": "Consecutive timestamped XML segments overlap.",
            })

    for gap in explicit_gaps:
        issues.append({
            "type": "EMPTY_TIMELINE_GAP",
            **gap,
            "severity": "MEDIUM",
            "source": ["XML"],
            "description": "The XML explicitly represents an empty or gap segment.",
        })
    for missing in explicit_missing:
        issues.append({
            "type": "MISSING_SEGMENT",
            **missing,
            "severity": "HIGH",
            "source": ["XML"],
            "description": "The XML explicitly marks media as missing or offline.",
        })

    if segments and video_duration is not None:
        timeline_end = max(segment["endTime"] for segment in segments)
        if abs(timeline_end - video_duration) > 1.0:
            issues.append({
                "type": "VIDEO_DURATION_MISMATCH",
                "startTime": min(timeline_end, video_duration),
                "endTime": max(timeline_end, video_duration),
                "duration": abs(timeline_end - video_duration),
                "severity": "LOW",
                "source": ["XML"],
                "description": f"XML timeline end ({timeline_end:.3f}s) differs from video duration ({video_duration:.3f}s).",
            })

    return {
        "supported": bool(segments),
        "elementNames": sorted(element_names),
        "segmentCount": len(segments),
        "segments": segments,
        "issues": issues,
        "warning": None if segments else "XML parsed, but no supported timestamped segments were found. Configure field mappings for this XML schema.",
    }


def probe_video(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise AnalysisError("FFprobe is required but was not found. Install FFmpeg and ensure ffprobe is on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("FFprobe timed out while reading the uploaded video.") from exc
    if result.returncode:
        detail = result.stderr.strip()[-1000:]
        raise AnalysisError(f"FFprobe could not read this video: {detail or 'unsupported or invalid media'}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AnalysisError("FFprobe returned invalid metadata.") from exc
    video_stream = next((stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise AnalysisError("The uploaded file does not contain a video stream.")
    duration = parse_time(video_stream.get("duration")) or parse_time(metadata.get("format", {}).get("duration"))
    frame_rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    return {
        "duration": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "frameRate": frame_rate,
        "videoCodec": video_stream.get("codec_name"),
        "audioCodecs": [stream.get("codec_name") for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"],
        "format": metadata.get("format", {}).get("format_name"),
        "size": parse_time(metadata.get("format", {}).get("size")),
    }


def detect_black_intervals(
    path: Path,
    pixel_threshold: float = 0.10,
    min_blackout_duration: float = 0.5,
    near_black_enabled: bool = True,
    timeout: int = 7200,
) -> list[dict[str, Any]]:
    """Run FFmpeg's frame-level blackdetect filter and normalize its intervals."""
    threshold = min(max(float(pixel_threshold), 0.001), 0.5) if near_black_enabled else min(float(pixel_threshold), 0.01)
    command = blackdetect_command(path, threshold)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AnalysisError("FFmpeg is required but was not found. Install FFmpeg and ensure ffmpeg is on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("Video analysis exceeded the configured processing time limit.") from exc

    intervals = parse_blackdetect_log(
        result.stderr,
        threshold,
        min_blackout_duration,
        near_black_enabled,
    )
    if result.returncode and not intervals:
        detail = result.stderr.strip()[-1200:]
        raise AnalysisError(f"FFmpeg failed to analyze the video: {detail or 'decoder error'}")
    return intervals


def blackdetect_command(path: Path, pixel_threshold: float, motion_log_path: Path | None = None) -> list[str]:
    filter_value = f"blackdetect=d=0:pix_th={pixel_threshold:.4f}"
    if motion_log_path:
        filter_value += f",deshake=rx=32:ry=32:filename={motion_log_path},blurdetect,metadata=mode=print:key=lavfi.blur"
    return ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", filter_value, "-an", "-f", "null", "-"]


def parse_blackdetect_log(
    stderr: str,
    threshold: float,
    min_blackout_duration: float,
    near_black_enabled: bool,
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    starts: list[float] = []
    for line in stderr.splitlines():
        start_match = re.search(r"black_start:\s*(-?[\d.]+)", line)
        end_match = re.search(r"black_end:\s*(-?[\d.]+)", line)
        if start_match:
            starts.append(max(0.0, float(start_match.group(1))))
        if end_match:
            start = starts.pop(0) if starts else max(0.0, float(end_match.group(1)))
            end = float(end_match.group(1))
            if end < start:
                continue
            duration = end - start
            if duration <= 0:
                continue
            is_blackout = duration >= min_blackout_duration
            intervals.append({
                "type": "BLACKOUT" if is_blackout else "BLACK_FRAME",
                "startTime": start,
                "endTime": end,
                "duration": duration,
                "severity": "HIGH" if is_blackout else "LOW",
                "source": ["VIDEO"],
                "description": "Sustained black interval detected in decoded video frames." if is_blackout else "Short black interval detected in decoded video frames.",
                "evidence": {
                    "method": "FFmpeg blackdetect",
                    "pixelThreshold": threshold,
                    "minimumBlackoutDuration": min_blackout_duration,
                    "nearBlackDetection": near_black_enabled,
                },
            })
    return intervals


def parse_deshake_log(
    motion_log: str,
    width: int | None,
    height: int | None,
    frame_rate: str | None,
    min_shake_duration: float = 0.5,
) -> list[dict[str, Any]]:
    """Report sustained high-frequency global motion from FFmpeg's deshake log."""
    if not frame_rate:
        raise AnalysisError("Could not determine a valid video frame rate for camera-shake detection.")
    try:
        fps = float(Fraction(frame_rate))
    except (ValueError, ZeroDivisionError) as exc:
        raise AnalysisError("Could not determine a valid video frame rate for camera-shake detection.") from exc
    if (
        not math.isfinite(fps)
        or fps <= 0
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise AnalysisError("Video dimensions and frame rate must be valid for camera-shake detection.")

    motion: list[tuple[float, float, float]] = []
    for line in motion_log.splitlines():
        if not line.strip() or line.lstrip().lower().startswith("ori x"):
            continue
        values = [value.strip() for value in line.split(",")]
        if len(values) != 12:
            raise AnalysisError("FFmpeg returned an unexpected camera-motion log format.")
        try:
            row = [float(value) for value in values]
        except ValueError as exc:
            raise AnalysisError("FFmpeg returned an invalid camera-motion measurement.") from exc
        if not all(math.isfinite(value) for value in row):
            raise AnalysisError("FFmpeg returned a non-finite camera-motion measurement.")
        motion.append((row[0], row[3], row[6]))

    if not motion:
        return []

    radius = max(1, round(fps * 0.25))
    prefix = [[0.0] for _ in range(3)]
    for sample in motion:
        for axis, value in enumerate(sample):
            prefix[axis].append(prefix[axis][-1] + value)

    threshold_pixels = max(1.5, min(width, height) * 0.002)
    rotation_radius = min(width, height) * 0.25
    shake_frames: list[tuple[int, float]] = []
    for index, (dx, dy, angle) in enumerate(motion):
        start = max(0, index - radius)
        end = min(len(motion), index + radius + 1)
        count = end - start
        average = [(prefix[axis][end] - prefix[axis][start]) / count for axis in range(3)]
        residual_x = dx - average[0]
        residual_y = dy - average[1]
        residual_angle = angle - average[2]
        magnitude = math.sqrt(
            residual_x**2 + residual_y**2 + (residual_angle * rotation_radius) ** 2
        )
        if magnitude >= threshold_pixels:
            shake_frames.append((index, magnitude))

    if not shake_frames:
        return []

    max_gap = max(1, round(fps * 0.1))
    groups: list[list[tuple[int, float]]] = [[shake_frames[0]]]
    for sample in shake_frames[1:]:
        if sample[0] - groups[-1][-1][0] <= max_gap + 1:
            groups[-1].append(sample)
        else:
            groups.append([sample])

    issues = []
    for group in groups:
        start_frame = group[0][0]
        end_frame = group[-1][0] + 1
        duration = (end_frame - start_frame) / fps
        if duration < min_shake_duration:
            continue
        peak_motion = max(magnitude for _, magnitude in group)
        issues.append({
            "type": "CAMERA_SHAKE",
            "startTime": start_frame / fps,
            "endTime": end_frame / fps,
            "duration": duration,
            "severity": "HIGH" if peak_motion >= threshold_pixels * 4 else "MEDIUM",
            "source": ["VIDEO"],
            "description": "Sustained sudden camera movement or shake detected in video.",
            "evidence": {
                "method": "FFmpeg deshake global-motion analysis",
                "minimumShakeDuration": min_shake_duration,
                "motionThresholdPixels": round(threshold_pixels, 3),
                "peakMotionPixels": round(peak_motion, 3),
            },
        })
    return issues


def parse_motion_blur_log(
    motion_log: str,
    blur_log: str,
    width: int | None,
    height: int | None,
    frame_rate: str | None,
    min_shake_duration: float = 0.5,
) -> list[dict[str, Any]]:
    """Report camera movement when the same frames show strong blur."""
    if not frame_rate:
        raise AnalysisError("Could not determine a valid video frame rate for camera-motion analysis.")
    try:
        fps = float(Fraction(frame_rate))
    except (ValueError, ZeroDivisionError) as exc:
        raise AnalysisError("Could not determine a valid video frame rate for camera-motion analysis.") from exc
    if (
        not math.isfinite(fps)
        or fps <= 0
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise AnalysisError("Video dimensions and frame rate must be valid for camera-motion analysis.")

    motion_rows = []
    for line in motion_log.splitlines():
        if not line.strip() or line.lstrip().lower().startswith("ori x"):
            continue
        values = [value.strip() for value in line.split(",")]
        if len(values) != 12:
            raise AnalysisError("FFmpeg returned an unexpected camera-motion log format.")
        try:
            row = [float(value) for value in values]
        except ValueError as exc:
            raise AnalysisError("FFmpeg returned an invalid camera-motion measurement.") from exc
        if not all(math.isfinite(value) for value in row):
            raise AnalysisError("FFmpeg returned a non-finite camera-motion measurement.")
        motion_rows.append(row)

    frame_blur: dict[int, float] = {}
    current_frame: int | None = None
    for line in blur_log.splitlines():
        frame_match = re.search(r"\bframe:\s*(\d+)", line)
        if frame_match:
            current_frame = int(frame_match.group(1))
        blur_match = re.search(r"\blavfi\.blur=([\d.eE+-]+)", line)
        if blur_match:
            if current_frame is None:
                raise AnalysisError("FFmpeg returned a blur score without a frame index.")
            try:
                score = float(blur_match.group(1))
            except ValueError as exc:
                raise AnalysisError("FFmpeg returned an invalid blur score.") from exc
            if not math.isfinite(score):
                raise AnalysisError("FFmpeg returned a non-finite blur score.")
            frame_blur[current_frame] = score

    if motion_rows and not frame_blur:
        raise AnalysisError("FFmpeg did not produce blur measurements for camera-motion analysis.")

    movement_threshold = max(1.5, min(width, height) * 0.002)
    blur_threshold = 5.5
    candidates = []
    for index, row in enumerate(motion_rows):
        blur_score = frame_blur.get(index)
        if blur_score is None:
            continue
        movement = math.sqrt(row[0] ** 2 + row[3] ** 2 + (row[6] * min(width, height) * 0.25) ** 2)
        if movement >= movement_threshold and blur_score >= blur_threshold:
            candidates.append((index, blur_score, movement))

    if not candidates:
        return []

    max_gap = max(1, round(fps * 0.1))
    groups: list[list[tuple[int, float, float]]] = [[candidates[0]]]
    for sample in candidates[1:]:
        if sample[0] - groups[-1][-1][0] <= max_gap + 1:
            groups[-1].append(sample)
        else:
            groups.append([sample])

    issues = []
    for group in groups:
        start_frame = group[0][0]
        end_frame = group[-1][0] + 1
        duration = (end_frame - start_frame) / fps
        if duration < min_shake_duration:
            continue
        issues.append({
            "type": "CAMERA_SHAKE",
            "startTime": start_frame / fps,
            "endTime": end_frame / fps,
            "duration": duration,
            "severity": "HIGH" if max(score for _, score, _ in group) >= blur_threshold * 2 else "MEDIUM",
            "source": ["VIDEO"],
            "description": "Camera movement with visible motion blur detected.",
            "evidence": {
                "method": "FFmpeg deshake motion and blurdetect",
                "minimumShakeDuration": min_shake_duration,
                "minimumBlurScore": blur_threshold,
                "peakBlurScore": round(max(score for _, score, _ in group), 3),
                "peakMotionPixels": round(max(movement for _, _, movement in group), 3),
            },
        })
    return issues


def aggregate_issues(video_issues: list[dict[str, Any]], xml_issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine closely matching findings of the same type without discarding evidence."""
    xml_interval_types = {"EMPTY_TIMELINE_GAP", "TIMELINE_DISCONTINUITY", "MISSING_SEGMENT"}
    video_interval_types = {"BLACKOUT", "BLACK_FRAME"}
    combined: list[dict[str, Any]] = []
    for issue in sorted(video_issues + xml_issues, key=lambda item: (item["startTime"], item["endTime"], item["type"])):
        match = next(
            (existing for existing in combined
             if (
                 existing["type"] == issue["type"]
                 or (
                     set(existing["source"]).isdisjoint(issue["source"])
                     and (
                         (existing["type"] in video_interval_types and issue["type"] in xml_interval_types)
                         or (issue["type"] in video_interval_types and existing["type"] in xml_interval_types)
                     )
                 )
             )
             and abs(existing["startTime"] - issue["startTime"]) <= 0.25
             and abs(existing["endTime"] - issue["endTime"]) <= 0.25),
            None,
        )
        if match:
            previous_sources = set(match["source"])
            previous_type = match["type"]
            match["source"] = sorted(set(match["source"] + issue["source"]))
            if match["type"] != issue["type"]:
                match["corroboratingTypes"] = sorted(
                    set(match.get("corroboratingTypes", []) + [previous_type, issue["type"]])
                )
                if "VIDEO" in issue["source"] and "VIDEO" not in previous_sources:
                    match["type"] = issue["type"]
                    for key in ("startTime", "endTime", "duration", "severity"):
                        match[key] = issue[key]
            match["description"] = f"{match['description']} Also identified by {issue['source'][0]} analysis."
        else:
            combined.append(dict(issue))
    for index, issue in enumerate(combined, 1):
        issue["id"] = f"issue-{index:04d}"
    return combined
