import time

from fastapi.testclient import TestClient

from main import _build_summary, app


def test_xml_is_required_and_video_is_optional():
    with TestClient(app) as client:
        missing_xml = client.post(
            "/api/analyze",
            files={"video": ("clip.mp4", b"not analyzed", "video/mp4")},
        )
        assert missing_xml.status_code == 422

        response = client.post(
            "/api/analyze",
            files={
                "xml": (
                    "timeline.xml",
                    b'<timeline><clip start="0" end="2" src="clip.mov"/></timeline>',
                    "application/xml",
                )
            },
        )
        assert response.status_code == 202
        job_id = response.json()["jobId"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in {"queued", "processing"}:
                break
            time.sleep(0.02)

        assert job["status"] == "completed", job
        assert job["result"]["video"] is None
        assert job["result"]["xml"]["supported"] is True
        assert job["result"]["summary"]["totalVideoDuration"] is None
        assert job["result"]["settings"]["cameraShakeEnabled"] is True
        assert job["result"]["settings"]["minCameraShakeDuration"] == 0.5


def test_camera_shake_detection_can_be_disabled():
    with TestClient(app) as client:
        response = client.post(
            "/api/analyze",
            data={"camera_shake_enabled": "false"},
            files={
                "xml": (
                    "timeline.xml",
                    b'<timeline><clip start="0" end="2" src="clip.mov"/></timeline>',
                    "application/xml",
                )
            },
        )
        assert response.status_code == 202
        job_id = response.json()["jobId"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in {"queued", "processing"}:
                break
            time.sleep(0.02)

        assert job["status"] == "completed", job
        assert job["result"]["settings"]["cameraShakeEnabled"] is False


def test_camera_shake_minimum_duration_is_saved_and_validated():
    with TestClient(app) as client:
        response = client.post(
            "/api/analyze",
            data={"min_camera_shake_duration": "1.2"},
            files={
                "xml": (
                    "timeline.xml",
                    b'<timeline><clip start="0" end="2" src="clip.mov"/></timeline>',
                    "application/xml",
                )
            },
        )
        assert response.status_code == 202
        job_id = response.json()["jobId"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in {"queued", "processing"}:
                break
            time.sleep(0.02)

        assert job["status"] == "completed", job
        assert job["result"]["settings"]["minCameraShakeDuration"] == 1.2

        invalid = client.post(
            "/api/analyze",
            data={"min_camera_shake_duration": "0"},
            files={
                "xml": (
                    "timeline.xml",
                    b'<timeline><clip start="0" end="2" src="clip.mov"/></timeline>',
                    "application/xml",
                )
            },
        )
        assert invalid.status_code == 422


def test_summary_counts_camera_shake_findings():
    summary = _build_summary(
        [
            {"type": "CAMERA_SHAKE", "duration": 0.8},
            {"type": "BLACKOUT", "duration": 1.2},
        ],
        10.0,
    )
    assert summary["cameraShakeCount"] == 1
    assert summary["totalIssues"] == 2
