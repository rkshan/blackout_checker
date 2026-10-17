from analysis import aggregate_issues, analyze_xml, parse_blackdetect_log, parse_time


def test_parse_time_supports_seconds_clock_and_units():
    assert parse_time("12.5") == 12.5
    assert parse_time("01:02:03.500") == 3723.5
    assert parse_time("250ms") == 0.25
    assert parse_time("invalid") is None


def test_xml_infers_gaps_and_overlaps_from_timestamped_segments():
    xml = b"""<timeline>
      <clip start="0" end="5" src="a.mov"/>
      <clip start="7" duration="2" src="b.mov"/>
      <clip start="8" end="10" src="c.mov"/>
    </timeline>"""
    result = analyze_xml(xml, video_duration=10)
    assert result["supported"] is True
    assert result["segmentCount"] == 3
    assert [issue["type"] for issue in result["issues"]] == ["TIMELINE_DISCONTINUITY", "TIMELINE_OVERLAP"]
    assert result["issues"][0]["duration"] == 2


def test_xml_does_not_claim_a_gap_when_no_segments_are_found():
    result = analyze_xml(b"<metadata><title>Untimed media</title></metadata>", 60)
    assert result["supported"] is False
    assert result["issues"] == []
    assert result["warning"]


def test_xml_detects_explicit_empty_and_missing_markers():
    xml = b"""<timeline><gap start="4" end="6"/>
      <missingSegment start="8" end="9"/></timeline>"""
    result = analyze_xml(xml)
    assert {issue["type"] for issue in result["issues"]} == {"EMPTY_TIMELINE_GAP", "MISSING_SEGMENT"}


def test_explicit_gap_is_not_duplicated_as_an_inferred_discontinuity():
    xml = b"""<timeline>
      <clip start="0" end="4" src="a.mov"/>
      <gap start="4" end="6"/>
      <clip start="6" end="8" src="b.mov"/>
    </timeline>"""
    result = analyze_xml(xml)
    assert [issue["type"] for issue in result["issues"]] == ["EMPTY_TIMELINE_GAP"]


def test_aggregation_merges_same_interval_without_losing_sources():
    video = [{"id": "v", "type": "BLACKOUT", "startTime": 1.0, "endTime": 2.0, "source": ["VIDEO"], "description": "Black"}]
    xml = [{"id": "x", "type": "BLACKOUT", "startTime": 1.1, "endTime": 2.1, "source": ["XML"], "description": "XML"}]
    result = aggregate_issues(video, xml)
    assert len(result) == 1
    assert result[0]["source"] == ["VIDEO", "XML"]
    assert result[0]["id"] == "issue-0001"


def test_aggregation_combines_matching_video_and_xml_interval_types():
    video = [{"type": "BLACKOUT", "startTime": 2.0, "endTime": 4.0, "duration": 2.0, "severity": "HIGH", "source": ["VIDEO"], "description": "Black interval."}]
    xml = [{"type": "EMPTY_TIMELINE_GAP", "startTime": 2.0, "endTime": 4.0, "duration": 2.0, "severity": "MEDIUM", "source": ["XML"], "description": "Explicit gap."}]
    result = aggregate_issues(video, xml)
    assert len(result) == 1
    assert result[0]["type"] == "BLACKOUT"
    assert result[0]["source"] == ["VIDEO", "XML"]
    assert result[0]["corroboratingTypes"] == ["BLACKOUT", "EMPTY_TIMELINE_GAP"]


def test_blackdetect_log_separates_blackouts_from_short_black_frames():
    log = """[blackdetect] black_start:1.0 black_end:2.0 black_duration:1.0
    [blackdetect] black_start:4.0 black_end:4.08 black_duration:0.08"""
    issues = parse_blackdetect_log(log, 0.1, 0.5, True)
    assert [issue["type"] for issue in issues] == ["BLACKOUT", "BLACK_FRAME"]
    assert round(issues[1]["duration"], 2) == 0.08
