# FrameChecker

A local-first video quality-control dashboard with a React/Vite/Tailwind frontend and a FastAPI backend. It analyzes decoded video frames using FFmpeg's `blackdetect` and `deshake` filters, reads video metadata with FFprobe, inspects supported XML segment timestamps, and exports normalized results to JSON or CSV.

## Requirements

- Node.js 18+ and npm
- Python 3.10+
- FFmpeg and FFprobe available on `PATH`

On macOS, use `python3` to check for Python. If it is not installed, install it with Homebrew (`brew install python`) or download the installer from [python.org](https://www.python.org/downloads/macos/). macOS may not provide a `python` command; that is expected.

Install FFmpeg on macOS with Homebrew:

```sh
brew install ffmpeg
ffmpeg -version
ffprobe -version
```

On Ubuntu/Debian use `sudo apt-get install ffmpeg`; on Windows install an FFmpeg build and add its `bin` directory to `PATH`.

## Run locally

From the project root, start the API. On macOS/Linux, use `python3` to create the virtual environment:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

On Windows PowerShell, use:

```powershell
cd backend
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

In a second terminal, start the UI:

```sh
npm install
npm run dev
```

Open the Vite URL (normally http://localhost:5173). Configure backend URL with `VITE_API_URL` only when the API is not at `http://localhost:8000`.

## Publish to the web (Vercel + Render)

The Vite frontend can be deployed as a static Vercel site. The API needs a long-running Render web service with FFmpeg; the repository-root `Dockerfile` installs FFmpeg and starts FastAPI.

> **Before making the API public:** the backend currently has no authentication or rate limiting. CORS only controls which browser pages can read responses; it does not prevent direct API requests. Add access protection before sharing the site publicly, or restrict access to trusted users.

1. Push the project to GitHub. In VS Code, review the files in **Source Control**, commit the intended changes, then push the branch you want to deploy.
2. In Render, choose **New → Web Service** and connect the GitHub repository. Select **Docker** as the runtime, leave **Root Directory** blank, and set **Dockerfile Path** to `./Dockerfile`.
3. Create the Render service. After the first deploy, open its URL plus `/api/health`; the response should be `{"status":"ok"}`.
4. In Vercel, choose **Add New → Project**, import the same repository, and keep the project root as `./`. Vercel should detect Vite; use `npm run build` as the build command and `dist` as the output directory.
5. In Vercel project settings, add `VITE_API_URL` with the Render service base URL, for example `https://your-api.onrender.com` (no trailing `/api`). Apply it to Production and deploy.
6. Copy the Vercel site's exact URL. In Render, open the API service's **Environment** settings and set `CORS_ORIGINS` to that URL, such as `https://your-site.vercel.app`. If you use more than one frontend domain, separate origins with commas. Save and redeploy.
7. Open the Vercel site, upload a small test video and XML timeline, and confirm an analysis completes. If the browser reports a CORS error, check that `CORS_ORIGINS` exactly matches the site origin, including `https://` and the hostname.

`VITE_API_URL` is embedded into the frontend at build time, so redeploy Vercel after changing it. Keep the Render service running while using the site; the API stores active jobs in memory and temporary upload files only for processing.

## Configuration

Copy `.env.example` to `backend/.env` to configure the backend:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | `3072` | Per-file upload limit (3 GiB; configured in mebibytes) |
| `MAX_ANALYSIS_SECONDS` | `7200` | FFmpeg processing timeout |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated frontend origins |

The upload endpoint requires an XML timeline (maximum 50 MB); a video is optional and accepts MP4, MOV, MXF, MKV, AVI, WebM, and MPEG. Without a video, the service runs XML timeline analysis only and does not require FFmpeg/FFprobe for that job. With a video, it checks metadata, black frames, and (when enabled in Detection settings) camera movement associated with visible motion blur, as well as sustained frame-to-frame shake. Camera-motion findings require the configured minimum duration (0.5 seconds by default; adjustable from 0.1 to 2 seconds). Motion blur is estimated from FFmpeg blur scores alongside global frame movement, so fast subject movement, intentional camera movement, low-texture scenes, or rolling-shutter distortion may affect results. Jobs are held in process memory and temporary server-side copies are deleted after completion, failure, or cancellation. Run only on a trusted machine/network; authentication and persistent job storage are not configured.

## Architecture and API

- `src/App.jsx`: responsive dashboard, upload controls, video preview, timeline, issue table, settings, and report downloads.
- `backend/main.py`: upload validation, asynchronous job lifecycle, status polling, cancellation, cleanup, and API.
- `backend/analysis.py`: FFprobe metadata, FFmpeg black-frame, camera-motion/blur, and camera-shake detection, XML parsing, timeline checks, and issue aggregation.
- `backend/tests/`: unit tests for timestamp parsing, XML segment analysis, and aggregation.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Service health |
| `POST` | `/api/analyze` | Start an analysis; required multipart field: `xml`; optional fields: `video`, `pixel_threshold`, `min_blackout_duration`, `near_black_enabled`, `camera_shake_enabled`, `min_camera_shake_duration`; returns `{ "jobId": "..." }` |
| `GET` | `/api/jobs/{jobId}` | Read job status and final result |
| `DELETE` | `/api/jobs/{jobId}` | Cancel a queued or running job |

Example request:

```sh
curl -X POST http://localhost:8000/api/analyze \
  -F 'xml=@/path/to/timeline.xml' \
  -F 'pixel_threshold=0.10' \
  -F 'min_blackout_duration=0.5' \
  -F 'near_black_enabled=true'
```

Add `-F 'video=@/path/to/video.mp4'` when frame-level video analysis is also wanted.

The response is `{"jobId":"<uuid>"}`. Poll `GET /api/jobs/<uuid>` for a job object such as `{"id":"<uuid>","status":"processing","stage":"Scanning video frames","result":null}`; a completed job sets `status` to `completed` and includes the result structure below.

Completed result contains `video` metadata, optional `xml` summary, effective `settings`, UTC `analysisDate`, normalized `issues`, `summary` statistics, `warnings`, and `limitations`. Each issue has `id`, `type`, `startTime`, `endTime`, `duration`, `severity`, `source`, and `description`; video findings also include detection evidence. CSV includes the media/configuration/summary header and full issue list; JSON exports the complete result object.

## Detection and XML assumptions

Black intervals come from FFmpeg's `blackdetect` filter over decoded video frames. The sensitivity control maps to FFmpeg `pix_th`; the minimum duration separates `BLACKOUT` from `BLACK_FRAME`. It is a technical detector, not a semantic assessment of whether a dark scene is intentional. Playback review remains important. Variable frame rates are handled by FFmpeg timestamps, but exact frame boundaries can vary by source/container and decoder.

Camera movement/blur analysis is an optional, more expensive full-resolution FFmpeg pass and is disabled by default to keep scans faster. Enable it in the dashboard when those findings are needed; it can take longer than black-frame detection, especially for high-resolution or long videos.

XML support is intentionally schema-adaptive, not universal. The parser recognizes common start/in, end/out, duration/length, and source/file attribute or child names. It compares timestamped segments for inferred gaps/overlaps and only reports `EMPTY_TIMELINE_GAP` or `MISSING_SEGMENT` where XML explicitly identifies them. It does not infer a gap from absent XML data. Unknown schemas are summarized with their element names and a warning rather than treated as a valid empty timeline. Adapt field mapping in `backend/analysis.py` when integrating a specific EDL or vendor schema; a representative XML sample is needed to support that schema accurately.

## Tests

```sh
cd backend
python -m pytest -q
```

Build the frontend with `npm run build`.

## Example

Example supported XML is in `backend/fixtures/sample-timeline.xml`, with an illustrative normalized result in `backend/fixtures/sample-analysis.json` (the app does not load fixture results). For instance, the first clip ends at 12.5 seconds and the next begins at 13 seconds, so the parser reports a 0.5-second `TIMELINE_DISCONTINUITY`; the explicit `<gap>` is separately reported as `EMPTY_TIMELINE_GAP`.
