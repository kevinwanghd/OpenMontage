"""URL 平台判定必须按 hostname 匹配，不能被子串欺骗。"""

from __future__ import annotations

from tools.analysis.url_kind import host_matches, host_of, platform_of


class TestHostMatching:

    def test_exact_and_subdomain(self):
        assert host_matches("youtube.com", "youtube.com")
        assert host_matches("www.youtube.com", "youtube.com")
        assert host_matches("m.YouTube.com", "youtube.com")

    def test_lookalike_domains_do_not_match(self):
        assert not host_matches("youtube.com.evil.com", "youtube.com")
        assert not host_matches("notyoutube.com", "youtube.com")
        assert not host_matches("", "youtube.com")

    def test_host_of_parses_and_normalizes(self):
        assert host_of("https://WWW.YouTube.com/x") == "www.youtube.com"
        assert host_of("youtube.com/x") == "youtube.com"
        assert host_of("") == ""


class TestPlatformOf:

    def test_youtube_and_shorts(self):
        assert platform_of("https://www.youtube.com/watch?v=1") == "youtube"
        assert platform_of("https://youtube.com/shorts/abc") == "shorts"
        assert platform_of("https://youtu.be/abc") == "youtube"

    def test_substring_in_query_is_not_platform(self):
        # 之前用 `"youtube.com" in url` 判断，这类 URL 会被误判
        assert platform_of("https://evil.com/?next=youtube.com/shorts") == "other_url"
        assert platform_of("https://youtube.com.evil.com/watch") == "other_url"

    def test_other_platforms(self):
        assert platform_of("https://www.instagram.com/p/x") == "instagram"
        assert platform_of("https://tiktok.com/@a/video/1") == "tiktok"
        assert platform_of("https://vimeo.com/1") == "vimeo"
        assert platform_of("https://x.com/a/status/1") == "twitter"
        assert platform_of("https://twitter.com/a/status/1") == "twitter"
        assert platform_of("https://example.com/a.mp4") == "other_url"

    def test_youtu_be_shorts_stays_youtube_for_analyzer(self):
        from tools.analysis.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        # 改造前 video_analyzer 只认 youtube.com/shorts；youtu.be/shorts 属于 youtube
        assert analyzer._detect_platform("https://youtu.be/shorts/1") == "youtube"
        assert analyzer._detect_platform("https://youtube.com/shorts/1") == "shorts"


class TestAnalyzerVocabulary:
    """video_analyzer 只对外暴露原有平台集合，其余归 other_url（避免下游未处理的新值）。"""

    def test_unsupported_platform_falls_back_to_other_url(self):
        from tools.analysis.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        assert analyzer._detect_platform("https://vimeo.com/1") == "other_url"
        assert analyzer._detect_platform("https://x.com/a/status/1") == "other_url"

    def test_supported_platforms_unchanged(self):
        from tools.analysis.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        assert analyzer._detect_platform("https://youtu.be/x") == "youtube"
        assert analyzer._detect_platform("https://youtube.com/shorts/1") == "shorts"
        assert analyzer._detect_platform("https://www.instagram.com/p/1") == "instagram"
