import time

from fastapi.testclient import TestClient

from main import app


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
