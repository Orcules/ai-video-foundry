"""Verify that Mux MP4 URLs always use 'highest.mp4', not the API-returned filename.

Mux API returns "name": "capped-1080p.mp4" in static_renditions.files[],
but that URL 404s. The actual download is always at "highest.mp4" (matching
the {"resolution": "highest"} we request).

This test mocks the Mux API to return "capped-1080p.mp4" and verifies we
still construct URLs with "highest.mp4".
"""

import json
import unittest
from unittest.mock import patch, MagicMock

# --- Test 1: mux_service.py upload_video() ---

class TestMuxUploadVideoMP4Name(unittest.TestCase):
    """MuxUploadService.upload_video() must use 'highest.mp4'."""

    @patch("api_pipeline.mux_service.requests")
    def test_upload_video_uses_highest_mp4(self, mock_requests):
        from api_pipeline.mux_service import MuxUploadService

        # Mock POST /uploads
        mock_upload_resp = MagicMock()
        mock_upload_resp.json.return_value = {
            "data": {"url": "https://upload.mux.com/fake", "id": "upload_123"}
        }

        # Mock GET /uploads/{id} → asset_id ready immediately
        mock_poll_resp = MagicMock()
        mock_poll_resp.ok = True
        mock_poll_resp.json.return_value = {
            "data": {"asset_id": "asset_456"}
        }

        # Mock GET /assets/{id} → ready with "capped-1080p.mp4" filename
        mock_asset_resp = MagicMock()
        mock_asset_resp.json.return_value = {
            "data": {
                "status": "ready",
                "static_renditions": {
                    "files": [
                        {"name": "capped-1080p.mp4", "status": "ready", "resolution": "highest"}
                    ]
                },
                "playback_ids": [{"id": "play_789"}],
            }
        }

        # Mock video download
        mock_dl_resp = MagicMock()
        mock_dl_resp.content = b"\x00" * 100

        # Wire up: post=upload, get=[download, poll, asset]
        mock_requests.post.return_value = mock_upload_resp
        mock_requests.get.side_effect = [mock_dl_resp, mock_poll_resp, mock_asset_resp]
        mock_requests.put.return_value = MagicMock()

        svc = MuxUploadService("token_id", "token_secret")
        result = svc.upload_video("https://example.com/video.mp4")

        self.assertIn("highest.mp4", result["mux_mp4_url"])
        self.assertNotIn("capped-1080p.mp4", result["mux_mp4_url"])
        self.assertEqual(result["mux_mp4_url"], "https://stream.mux.com/play_789/highest.mp4")


# --- Test 2: mux_service.py upload_local_file() ---

class TestMuxUploadLocalFileMP4Name(unittest.TestCase):
    """MuxUploadService.upload_local_file() must use 'highest.mp4'."""

    @patch("builtins.open", unittest.mock.mock_open(read_data=b"\x00" * 100))
    @patch("api_pipeline.mux_service.requests")
    def test_upload_local_file_uses_highest_mp4(self, mock_requests):
        from api_pipeline.mux_service import MuxUploadService

        mock_upload_resp = MagicMock()
        mock_upload_resp.json.return_value = {
            "data": {"url": "https://upload.mux.com/fake", "id": "upload_123"}
        }

        mock_poll_resp = MagicMock()
        mock_poll_resp.ok = True
        mock_poll_resp.json.return_value = {"data": {"asset_id": "asset_456"}}

        mock_asset_resp = MagicMock()
        mock_asset_resp.json.return_value = {
            "data": {
                "status": "ready",
                "static_renditions": {
                    "files": [
                        {"name": "capped-1080p.mp4", "status": "ready"}
                    ]
                },
                "playback_ids": [{"id": "play_abc"}],
            }
        }

        mock_requests.post.return_value = mock_upload_resp
        mock_requests.get.side_effect = [mock_poll_resp, mock_asset_resp]
        mock_requests.put.return_value = MagicMock()

        svc = MuxUploadService("token_id", "token_secret")
        result = svc.upload_local_file("/tmp/fake.mp4")

        self.assertIn("highest.mp4", result["mux_mp4_url"])
        self.assertNotIn("capped-1080p.mp4", result["mux_mp4_url"])


# --- Test 3: server.py fallback loop constructs correct URL ---

class TestMuxFallbackLoopMP4Name(unittest.TestCase):
    """The _mux_fallback_loop code path must use 'highest.mp4'.

    We can't easily test the full loop (it runs in a daemon thread),
    but we can verify the hardcoded value is in the source.
    """

    def test_server_fallback_hardcodes_highest(self):
        import os
        server_path = os.path.join(os.path.dirname(__file__), "..", "server.py")
        with open(server_path) as f:
            source = f.read()
        # The fallback loop should have mp4_name = "highest.mp4"
        self.assertIn('mp4_name = "highest.mp4"', source)
        # And should NOT have the old rend_files[0]["name"] pattern
        self.assertNotIn('rend_files[0]["name"]', source)


# --- Test 4: sim_pipeline_runner uses highest.mp4 ---

class TestSimPipelineRunnerMP4Name(unittest.TestCase):
    """sim_pipeline_runner must use 'highest.mp4' not 'high.mp4'."""

    def test_sim_runner_uses_highest(self):
        import inspect
        from api_pipeline.services import sim_pipeline_runner
        source = inspect.getsource(sim_pipeline_runner)
        self.assertIn("/highest.mp4", source)
        self.assertNotIn("/high.mp4", source)


if __name__ == "__main__":
    unittest.main()
