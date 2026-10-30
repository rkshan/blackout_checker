import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle, ArrowDownToLine, Check, Clapperboard, Clock3,
  FileCode2, FileVideo2, FolderOpen, LoaderCircle, Play, RotateCcw,
  ScanLine, Search, ShieldCheck, SlidersHorizontal, UploadCloud, X,
} from "lucide-react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

function formatTime(seconds = 0) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Number(seconds);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = (value % 60).toFixed(3).padStart(6, "0");
  return `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${String(minutes).padStart(2, "0")}:${secs}`;
}

function formatSize(size = 0) {
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const power = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`;
}

function FileCard({ label, file, accept, onChange, onRemove, icon: Icon, hint, required = false }) {
  const input = useRef(null);
  const [dragging, setDragging] = useState(false);
  const receive = (selected) => {
    const candidate = selected?.[0];
    if (candidate) onChange(candidate);
  };
  return (
    <div className={`upload-card ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
      onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => { event.preventDefault(); setDragging(false); receive(event.dataTransfer.files); }}>
      <input ref={input} type="file" accept={accept} hidden onChange={(event) => receive(event.target.files)} />
      <div className="upload-card-head">
        <div className="upload-icon"><Icon size={18} /></div>
        <div><h3>{label}</h3><p>{hint}</p></div>
        <span className={`file-state ${file ? "ready" : ""}`}>{file ? "READY" : required ? "REQUIRED" : "OPTIONAL"}</span>
      </div>
      {file ? (
        <div className="selected-file">
          <div className="file-chip"><Icon size={18} /></div>
          <div className="file-info"><strong title={file.name}>{file.name}</strong><span>{formatSize(file.size)} · {file.name.split(".").pop().toUpperCase()}</span></div>
          <button className="icon-button remove-file" aria-label={`Remove ${label}`} onClick={onRemove}><X size={16} /></button>
        </div>
      ) : (
        <div className="drop-zone">
          <UploadCloud size={22} />
          <span>Drop a file here or <button className="text-button" onClick={() => input.current?.click()}>browse files</button></span>
          <button className="browse-button" onClick={() => input.current?.click()}><FolderOpen size={15} /> Browse</button>
        </div>
      )}
    </div>
  );
}

function App() {
  const [video, setVideo] = useState(null);
  const [xml, setXml] = useState(null);
  const [threshold, setThreshold] = useState(0.1);
  const [minDuration, setMinDuration] = useState(0.5);
  const [nearBlack, setNearBlack] = useState(true);
  const [cameraShakeEnabled, setCameraShakeEnabled] = useState(false);
  const [minShakeDuration, setMinShakeDuration] = useState(0.5);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [severityFilter, setSeverityFilter] = useState("ALL");
  const [timeStart, setTimeStart] = useState("");
  const [timeEnd, setTimeEnd] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("startTime");
  const [videoUrl, setVideoUrl] = useState("");
  const videoRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    if (!video) { setVideoUrl(""); return undefined; }
    const url = URL.createObjectURL(video);
    setVideoUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [video]);

  useEffect(() => () => clearInterval(pollRef.current), []);

  const status = job?.status || "idle";
  const busy = status === "queued" || status === "processing";
  const result = job?.result;
  const issues = result?.issues || [];
  const duration = result?.video?.duration
    ?? Math.max(0, ...(result?.xml?.segments || []).map((segment) => segment.endTime));
  const stats = useMemo(() => ({
    blackouts: result?.summary?.blackoutCount ?? issues.filter((item) => item.type === "BLACKOUT").length,
    shakes: result?.summary?.cameraShakeCount ?? issues.filter((item) => item.type === "CAMERA_SHAKE").length,
    frames: result?.summary?.blackFrameCount ?? issues.filter((item) => item.type === "BLACK_FRAME").length,
    gaps: result?.summary?.timelineGapCount ?? issues.filter((item) => ["EMPTY_TIMELINE_GAP", "TIMELINE_DISCONTINUITY"].includes(item.type)).length,
    missing: result?.summary?.missingOrDiscontinuousCount ?? issues.filter((item) => item.type === "MISSING_SEGMENT").length,
    blackDuration: result?.summary?.confirmedBlackIntervalDuration ?? issues.filter((item) => item.source.includes("VIDEO")).reduce((total, item) => total + item.duration, 0),
    gapDuration: result?.summary?.identifiedTimelineGapDuration ?? issues.filter((item) => ["EMPTY_TIMELINE_GAP", "TIMELINE_DISCONTINUITY"].includes(item.type)).reduce((total, item) => total + item.duration, 0),
  }), [issues, result]);

  useEffect(() => {
    if (!jobId || !busy) return undefined;
    clearInterval(pollRef.current);
    const poll = async () => {
      try {
        const response = await fetch(`${API}/api/jobs/${jobId}`);
        if (response.status === 404) {
          const message = "The backend no longer has this analysis job. It may have restarted during processing; upload the files and run the analysis again.";
          clearInterval(pollRef.current);
          setJob({ status: "failed", stage: "Could not load results", error: message });
          setError(message);
          return;
        }
        if (!response.ok) {
          const message = `Could not load analysis status (HTTP ${response.status}).`;
          if (response.status < 500 && response.status !== 429) {
            clearInterval(pollRef.current);
            setJob({ status: "failed", stage: "Could not load results", error: message });
            setError(message);
            return;
          }
          throw new Error(message);
        }
        const nextJob = await response.json();
        setJob(nextJob);
        setError("");
        if (!["queued", "processing"].includes(nextJob.status)) clearInterval(pollRef.current);
      } catch (pollError) {
        setError(pollError instanceof Error && pollError.message.startsWith("Could not load analysis status")
          ? `${pollError.message} Retrying automatically.`
          : "Connection to the analysis service was interrupted. Retrying automatically.");
      }
    };
    poll();
    pollRef.current = setInterval(poll, 1200);
    return () => clearInterval(pollRef.current);
  }, [jobId, busy]);

  const filteredIssues = useMemo(() => issues
    .filter((item) => filter === "ALL" || item.type === filter)
    .filter((item) => severityFilter === "ALL" || item.severity === severityFilter)
    .filter((item) => timeStart === "" || item.startTime >= Number(timeStart))
    .filter((item) => timeEnd === "" || item.startTime <= Number(timeEnd))
    .filter((item) => `${item.type} ${item.description} ${item.source.join(" ")}`.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => sort === "severity"
      ? ({ HIGH: 0, MEDIUM: 1, LOW: 2 }[a.severity] - { HIGH: 0, MEDIUM: 1, LOW: 2 }[b.severity])
      : a[sort] - b[sort]), [issues, filter, severityFilter, timeStart, timeEnd, query, sort]);

  const startAnalysis = async () => {
    if (!xml) { setError("Select an XML timeline file to start analysis."); return; }
    setError("");
    setJob(null);
    setJobId(null);
    const form = new FormData();
    if (video) form.append("video", video);
    form.append("xml", xml);
    form.append("pixel_threshold", String(threshold));
    form.append("min_blackout_duration", String(minDuration));
    form.append("near_black_enabled", String(nearBlack));
    form.append("camera_shake_enabled", String(cameraShakeEnabled));
    form.append("min_camera_shake_duration", String(minShakeDuration));
    try {
      const response = await fetch(`${API}/api/analyze`, { method: "POST", body: form });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not start analysis.");
      setJobId(body.jobId);
      setJob({ status: "queued", stage: "Waiting to start" });
    } catch (uploadError) {
      setError(uploadError.message.includes("Failed to fetch")
        ? "Unable to reach the analysis service. Start the backend at http://localhost:8000."
        : uploadError.message);
    }
  };

  const cancelAnalysis = async () => {
    if (!jobId) return;
    try {
      await fetch(`${API}/api/jobs/${jobId}`, { method: "DELETE" });
      setJob((current) => ({ ...current, status: "cancelled", stage: "Analysis cancelled" }));
    } catch {
      setError("Could not cancel analysis. Check the backend connection.");
    }
  };

  const reset = () => {
    if (busy) cancelAnalysis();
    setJobId(null); setJob(null); setError(""); setFilter("ALL"); setSeverityFilter("ALL"); setTimeStart(""); setTimeEnd(""); setQuery("");
  };

  const seek = (time) => {
    if (videoRef.current) { videoRef.current.currentTime = time; videoRef.current.play().catch(() => {}); }
  };

  const exportFile = (format) => {
    if (!result) return;
    let contents;
    let type;
    let filename = "video-qc-report";
    if (format === "json") {
      contents = JSON.stringify(result, null, 2);
      type = "application/json";
      filename += ".json";
    } else {
      const columns = ["id", "type", "startTime", "endTime", "duration", "severity", "source", "description"];
      const escape = (value) => `"${String(Array.isArray(value) ? value.join("; ") : value ?? "").replaceAll('"', '""')}"`;
      const reportRows = [
        ["FrameChecker report"],
        ["Video file", result.video?.filename || "Not supplied"],
        ["XML file", result.xml?.filename || "Not supplied"],
        ["Analysis date", result.analysisDate],
        ["Video duration (s)", result.summary.totalVideoDuration],
        ["Resolution", result.video ? `${result.video.width || "?"} x ${result.video.height || "?"}` : "Not available"],
        ["Frame rate", result.video?.frameRate || "Not available"],
        ["Video codec", result.video?.videoCodec || "Not available"],
        ["Pixel threshold", result.settings.pixelThreshold],
        ["Minimum blackout duration (s)", result.settings.minBlackoutDuration],
        ["Near-black detection", result.settings.nearBlackEnabled],
        ["Total findings", result.summary.totalIssues],
        ["Blackouts", result.summary.blackoutCount],
        ["Camera shakes", result.summary.cameraShakeCount],
        ["Black frames", result.summary.blackFrameCount],
        ["Timeline gaps", result.summary.timelineGapCount],
        ["Missing/discontinuous segments", result.summary.missingOrDiscontinuousCount],
        ["Confirmed black interval duration (s)", result.summary.confirmedBlackIntervalDuration],
        ["Identified timeline gap duration (s)", result.summary.identifiedTimelineGapDuration],
        ["Warnings", result.warnings.join("; ") || "None"],
        ["Limitations", result.limitations.join("; ")],
        [],
        columns,
        ...issues.map((item) => columns.map((column) => item[column])),
      ];
      contents = reportRows.map((row) => row.map(escape).join(",")).join("\n");
      type = "text/csv";
      filename += ".csv";
    }
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([contents], { type }));
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <main className="app-shell min-h-screen">
      <header className="topbar">
        <a className="brand" href="#" aria-label="FrameChecker home"><span className="brand-mark"><ScanLine size={18} /></span><span>FrameChecker</span></a>
        <div className="topbar-right"><ShieldCheck size={14} /><span>Private media analysis</span></div>
      </header>

      <section className="page-heading">
        <div><h1>Video quality check</h1><p>Check a video and timeline for blackouts and continuity issues.</p></div>
        <div className={`status-pill status-${status}`}><span className="status-dot" />{status === "idle" ? "Ready to analyze" : status === "processing" || status === "queued" ? "Analysis in progress" : status === "completed" ? "Analysis complete" : status === "failed" ? "Analysis failed" : "Analysis cancelled"}</div>
      </section>

      {error && <div className="alert error-alert"><AlertCircle size={18} /><span>{error}</span><button className="icon-button" onClick={() => setError("")} aria-label="Dismiss error"><X size={16} /></button></div>}

      <div className="workspace-grid">
        <div className="main-column">
          <section className="panel">
            <div className="section-title"><div><div><h2>Files</h2><p>Choose a timeline. Add a video for frame-level checks.</p></div></div></div>
            <div className="upload-grid">
              <FileCard label="Video file" file={video} accept="video/*,.mxf" icon={FileVideo2} hint="Optional · Detects black frames and camera movement blur" onChange={setVideo} onRemove={() => setVideo(null)} />
              <FileCard label="XML timeline" file={xml} accept=".xml,text/xml,application/xml" icon={FileCode2} hint="Required · Segment and edit metadata" required onChange={setXml} onRemove={() => setXml(null)} />
            </div>
          </section>

          {(video || result) && <section className="panel preview-panel">
            <div className="section-title"><div><div><h2>Preview</h2><p>Review detected moments in context.</p></div></div>{result?.video && <span className="subtle-label">{result.video.width} × {result.video.height} · {result.video.videoCodec?.toUpperCase()}</span>}</div>
            {videoUrl ? <div className="video-wrap"><video ref={videoRef} src={videoUrl} controls preload="metadata" /></div> : <div className="video-placeholder"><Clapperboard size={25} /><span>{video ? "Video preview is available for this browser session." : "No video uploaded · showing XML timeline findings."}</span></div>}
            {result && <div className="timeline">
              <div className="timeline-heading"><span>DETECTION TIMELINE</span><span>{formatTime(duration)}</span></div>
              <div className="timeline-track">
                {issues.map((item) => {
                  const left = duration ? Math.max(0, Math.min(100, item.startTime / duration * 100)) : 0;
                  const width = duration ? Math.max(0.35, Math.min(100 - left, item.duration / duration * 100)) : 1;
                  return <button key={item.id} title={`${item.type} · ${formatTime(item.startTime)}`} className={`timeline-marker ${item.type.toLowerCase()}`} style={{ left: `${left}%`, width: `${width}%` }} onClick={() => seek(item.startTime)} aria-label={`Seek to ${item.type} at ${formatTime(item.startTime)}`} />;
                })}
              </div>
              <div className="timeline-ticks"><span>00:00</span><span>{formatTime(duration / 4)}</span><span>{formatTime(duration / 2)}</span><span>{formatTime(duration * 0.75)}</span><span>{formatTime(duration)}</span></div>
              <div className="timeline-legend"><span><i className="legend-blackout" />Blackout / black frame</span><span><i className="legend-shake" />Camera movement / blur</span><span><i className="legend-gap" />XML gap / discontinuity</span><span><i className="legend-other" />Other XML finding</span></div>
            </div>}
          </section>}

          <section className="panel results-panel">
            <div className="section-title"><div><div><h2>Findings</h2><p>{result ? `${issues.length} findings` : "Run an analysis to see the report."}</p></div></div>{result && <div className="export-group"><button className="outline-button" onClick={() => exportFile("csv")}><ArrowDownToLine size={15} /> CSV</button><button className="outline-button" onClick={() => exportFile("json")}><ArrowDownToLine size={15} /> JSON</button></div>}</div>
            {result?.warnings?.map((warning) => <div className="alert warning-alert" key={warning}><AlertCircle size={16} />{warning}</div>)}
            {result ? <>
              <div className="table-toolbar"><div className="search-box"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search findings..." /></div><label className="filter-select"><SlidersHorizontal size={14} /><select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="ALL">All issue types</option>{[...new Set(issues.map((item) => item.type))].map((type) => <option key={type}>{type}</option>)}</select></label><label className="filter-select"><select aria-label="Filter by severity" value={severityFilter} onChange={(event) => setSeverityFilter(event.target.value)}><option value="ALL">All severity</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option></select></label><label className="filter-select"><span>Sort</span><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="startTime">Start time</option><option value="duration">Duration</option><option value="severity">Severity</option></select></label><label className="time-filter"><span>From s</span><input type="number" min="0" value={timeStart} onChange={(event) => setTimeStart(event.target.value)} aria-label="Filter from start time in seconds" /></label><label className="time-filter"><span>To s</span><input type="number" min="0" value={timeEnd} onChange={(event) => setTimeEnd(event.target.value)} aria-label="Filter to start time in seconds" /></label></div>
              <div className="table-wrap"><table><thead><tr><th>Issue type</th><th>Start time</th><th>End time</th><th>Duration</th><th>Severity</th><th>Source</th><th /></tr></thead><tbody>
                {filteredIssues.map((item) => <tr key={item.id}><td><span className={`issue-label ${item.type.toLowerCase()}`}><i />{item.type === "CAMERA_SHAKE" ? "Camera movement / blur" : item.type.replaceAll("_", " ")}</span></td><td className="mono">{formatTime(item.startTime)}</td><td className="mono">{formatTime(item.endTime)}</td><td className="mono">{item.duration.toFixed(3)} s</td><td><span className={`severity severity-${item.severity.toLowerCase()}`}>{item.severity}</span></td><td><span className="source-tag">{item.source.join(" + ")}</span></td><td><button className="view-button" onClick={() => seek(item.startTime)}><Play size={12} fill="currentColor" /> View</button></td></tr>)}
                {!filteredIssues.length && <tr><td colSpan="7" className="empty-row">{issues.length ? "No findings match your search." : "No issues detected. Your video passed the configured checks."}</td></tr>}
              </tbody></table></div>
              <div className="table-footer">Showing {filteredIssues.length} of {issues.length} findings <span>Times shown as HH:MM:SS.mmm</span></div>
            </> : <div className="empty-state"><div className="empty-state-icon"><ScanLine size={22} /></div><strong>Your report will appear here</strong><span>Upload a video and start analysis to see detected issues.</span></div>}
          </section>
        </div>

        <aside className="side-column">
          <section className="panel settings-panel">
            <div className="section-title compact"><div><div><h2>Detection settings</h2><p>Choose which video checks to run.</p></div></div></div>
            <div className="setting-row"><div className="setting-heading"><label htmlFor="threshold">Black pixel threshold</label><span>{Math.round(threshold * 100)}%</span></div><input id="threshold" type="range" min="0.01" max="0.3" step="0.01" value={threshold} onChange={(event) => setThreshold(Number(event.target.value))} /><div className="range-labels"><span>Strict</span><span>Sensitive</span></div><small>Used for video frame analysis when an optional video is uploaded.</small></div>
            <div className="setting-row"><div className="setting-heading"><label htmlFor="duration">Minimum blackout</label><span>{minDuration.toFixed(1)} s</span></div><input id="duration" type="range" min="0" max="5" step="0.1" value={minDuration} onChange={(event) => setMinDuration(Number(event.target.value))} /><div className="range-labels"><span>0 s</span><span>5 s</span></div><small>Shorter detections are reported as black frames when video is provided.</small></div>
            <label className="toggle-row"><span><strong>Near-black detection</strong><small>Include frames that are very dark, not just pure black.</small></span><input type="checkbox" checked={nearBlack} onChange={(event) => setNearBlack(event.target.checked)} /><i /></label>
            <label className="toggle-row"><span><strong>Camera movement / blur</strong><small>Slower full-frame scan for movement and visible blur. Off by default for faster analysis.</small></span><input type="checkbox" checked={cameraShakeEnabled} onChange={(event) => setCameraShakeEnabled(event.target.checked)} /><i /></label>
            <div className="setting-row shake-duration-setting"><div className="setting-heading"><label htmlFor="shake-duration">Minimum duration</label><span>{minShakeDuration.toFixed(1)} s</span></div><input id="shake-duration" type="range" min="0.1" max="2" step="0.1" value={minShakeDuration} disabled={!cameraShakeEnabled} onChange={(event) => setMinShakeDuration(Number(event.target.value))} /><div className="range-labels"><span>0.1 s · more sensitive</span><span>2 s · less sensitive</span></div><small>Movement and blur must last at least this long to be reported.</small></div>
            <div className="settings-note"><AlertCircle size={15} /><span>Dark scenes may be flagged. Review detections in the video preview before classifying as a blackout.</span></div>
            <div className="action-buttons">
              {busy ? <button className="primary-button cancel-button" onClick={cancelAnalysis}><X size={16} /> Cancel analysis</button> :               <button className="primary-button" onClick={startAnalysis} disabled={!xml}><ScanLine size={16} /> Start analysis</button>}
              <button className="reset-button" onClick={reset}><RotateCcw size={14} /> Reset</button>
            </div>
            {busy && <div className="progress-area"><div className="progress-heading"><span><LoaderCircle size={14} className="spin" />{job?.stage || "Preparing analysis"}</span><span>{status === "queued" ? "QUEUED" : "RUNNING"}</span></div><div className="progress-track"><i /></div><small>Large files may take several minutes.</small></div>}
            {status === "failed" && <div className="job-error"><AlertCircle size={15} />{job.error}</div>}
            {status === "completed" && <div className="success-note"><Check size={15} />Analysis finished successfully.</div>}
          </section>

          {result && <section className="panel summary-panel"><div className="section-title compact"><div><div><h2>Summary</h2><p>Analysis overview.</p></div></div></div>
            <div className="summary-total"><span>Total findings</span><strong>{issues.length}</strong><small>Video duration · {formatTime(duration)}</small></div>
            <div className="summary-list"><div><span><i className="dot dot-red" />Blackouts</span><b>{stats.blackouts}</b></div><div><span><i className="dot dot-orange" />Black frames</span><b>{stats.frames}</b></div><div><span><i className="dot dot-shake" />Camera movement / blur</span><b>{stats.shakes}</b></div><div><span><i className="dot dot-blue" />Timeline gaps</span><b>{stats.gaps}</b></div><div><span><i className="dot dot-purple" />Missing / discontinuous</span><b>{stats.missing}</b></div></div>
            <div className="summary-durations"><div><small>Black interval duration</small><strong>{stats.blackDuration.toFixed(3)} s</strong></div><div><small>Timeline gap duration</small><strong>{stats.gapDuration.toFixed(3)} s</strong></div></div>
            <div className="report-date"><Clock3 size={13} /> Analyzed {new Date(result.analysisDate).toLocaleString()}</div>
          </section>}
          <div className="privacy-note"><ShieldCheck size={16} /><span><strong>Private by design</strong>Your media is analyzed locally and temporary server copies are removed when processing finishes.</span></div>
        </aside>
      </div>
      <footer><span>Developed By Rukshan S. 2026</span></footer>
    </main>
  );
}

export default App;
