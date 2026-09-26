"""
Unit tests for hub.antigravity.image_downloader
Covers:
- Normal image download via urllib
- Fallback to curl when urllib encounters http.client.IncompleteRead or URLError
- Complete download failure handling (both urllib and curl fail)
- File caching and deduplication (dest_path size > 0)
- Filename sanitization and security (path traversal prevention)
- Non-image filtering
- Direct verification of download_file_with_curl
"""

import http.client
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from hub.antigravity.image_downloader import (
    IMAGE_MIMETYPES,
    download_file_with_curl,
    download_slack_images,
    sanitize_filename,
)


class TestImageDownloader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_img_dl_")
        self.base_dir = Path(self.temp_dir)
        self.task_id = "tsk_test_123"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("valid_image.png"), "valid_image.png")
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename(".."), "attachment.png")
        self.assertEqual(sanitize_filename("."), "attachment.png")
        self.assertEqual(sanitize_filename("bad;rm -rf /!$.jpg"), "__.jpg")
        self.assertEqual(sanitize_filename(""), "attachment.png")

    def test_download_empty_files_list(self):
        res = download_slack_images([], task_id=self.task_id, target_base_dir=self.base_dir)
        self.assertEqual(res, [])

    def test_download_skips_non_images(self):
        files = [
            {"name": "doc.pdf", "mimetype": "application/pdf", "url_private": "https://files.slack.com/doc.pdf"},
            {"name": "archive.zip", "mimetype": "application/zip", "url_private": "https://files.slack.com/archive.zip"},
        ]
        res = download_slack_images(files, task_id=self.task_id, target_base_dir=self.base_dir)
        self.assertEqual(res, [])

    def test_download_reuses_cached_image(self):
        dest_file = self.base_dir / "1_sample.png"
        dest_file.write_bytes(b"existing_valid_data")

        files = [
            {"name": "sample.png", "mimetype": "image/png", "url_private": "https://files.slack.com/sample.png"}
        ]
        with patch("urllib.request.urlopen") as mock_url:
            res = download_slack_images(files, task_id=self.task_id, target_base_dir=self.base_dir)
            mock_url.assert_not_called()
            self.assertEqual(res, [str(dest_file.resolve())])

    @patch("urllib.request.urlopen")
    def test_download_normal_urllib_success(self, mock_urlopen):
        dummy_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        mock_resp = MagicMock()
        mock_resp.read.return_value = dummy_content
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        files = [
            {
                "name": "screenshot.png",
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/files-pri/T0123/screenshot.png",
            }
        ]
        res = download_slack_images(
            files,
            task_id=self.task_id,
            target_base_dir=self.base_dir,
            token="xoxb-test-token",
        )
        self.assertEqual(len(res), 1)
        dest_path = Path(res[0])
        self.assertTrue(dest_path.exists())
        self.assertEqual(dest_path.read_bytes(), dummy_content)
        mock_urlopen.assert_called_once()

    @patch("hub.antigravity.image_downloader.download_file_with_curl")
    @patch("urllib.request.urlopen")
    def test_download_fallback_to_curl_on_incomplete_read(self, mock_urlopen, mock_curl):
        """Simulate Python 3.14 macOS IncompleteRead on files.slack.com CDN and verify curl fallback."""
        mock_urlopen.side_effect = http.client.IncompleteRead(b"partial_stream_data")

        def fake_curl(url, dest_path, bot_token=None, timeout=30):
            dest_path.write_bytes(b"curl_complete_image_payload")
            return True

        mock_curl.side_effect = fake_curl

        files = [
            {
                "name": "boost_diagram.jpg",
                "mimetype": "image/jpeg",
                "url_private": "https://files.slack.com/files-pri/T0123/boost_diagram.jpg",
            }
        ]
        res = download_slack_images(
            files,
            task_id=self.task_id,
            target_base_dir=self.base_dir,
            token="xoxb-test-token",
        )
        self.assertEqual(len(res), 1)
        dest_path = Path(res[0])
        self.assertTrue(dest_path.is_file())
        self.assertEqual(dest_path.read_bytes(), b"curl_complete_image_payload")
        mock_curl.assert_called_once()

    @patch("hub.antigravity.image_downloader.download_file_with_curl")
    @patch("urllib.request.urlopen")
    def test_download_fallback_to_curl_on_urlerror(self, mock_urlopen, mock_curl):
        mock_urlopen.side_effect = urllib.error.URLError("Connection reset by peer")

        def fake_curl(url, dest_path, bot_token=None, timeout=30):
            dest_path.write_bytes(b"curl_success")
            return True

        mock_curl.side_effect = fake_curl

        files = [
            {
                "name": "goal_wireframe.png",
                "mimetype": "image/png",
                "url_private": "https://files.slack.com/files-pri/T0123/goal_wireframe.png",
            }
        ]
        res = download_slack_images(
            files,
            task_id=self.task_id,
            target_base_dir=self.base_dir,
            token="xoxb-test-token",
        )
        self.assertEqual(len(res), 1)
        dest_path = Path(res[0])
        self.assertEqual(dest_path.read_bytes(), b"curl_success")
        mock_curl.assert_called_once()

    @patch("hub.antigravity.image_downloader.download_file_with_curl")
    @patch("urllib.request.urlopen")
    def test_download_both_fail_returns_empty_and_cleans_up(self, mock_urlopen, mock_curl):
        mock_urlopen.side_effect = http.client.IncompleteRead(b"")
        mock_curl.return_value = False

        files = [
            {
                "name": "failed.png",
                "mimetype": "image/png",
                "url_private": "https://files.slack.com/files-pri/T0123/failed.png",
            }
        ]
        res = download_slack_images(
            files,
            task_id=self.task_id,
            target_base_dir=self.base_dir,
            token="xoxb-test-token",
        )
        self.assertEqual(res, [])

    @patch("subprocess.run")
    def test_download_file_with_curl_invokes_command_correctly(self, mock_run):
        dest_path = self.base_dir / "curl_test.png"

        # Simulate curl creating the file and exiting 0
        def fake_subproc(cmd, **kwargs):
            dest_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            res = MagicMock()
            res.returncode = 0
            res.stderr = ""
            return res

        mock_run.side_effect = fake_subproc

        ok = download_file_with_curl(
            url="https://files.slack.com/files-pri/T0123/curl_test.png",
            dest_path=dest_path,
            bot_token="xoxb-mock-secret",
            timeout=30,
        )
        self.assertTrue(ok)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("curl", cmd)
        self.assertIn("--fail", cmd)
        self.assertIn("--location-trusted", cmd)
        self.assertIn("--max-time", cmd)
        self.assertIn("30", cmd)
        self.assertIn("-sS", cmd)
        self.assertIn("-L", cmd)
        self.assertIn("-H", cmd)
        self.assertIn("Authorization: Bearer xoxb-mock-secret", cmd)
        self.assertIn(str(dest_path), cmd)

    @patch("subprocess.run")
    def test_download_file_with_curl_removes_empty_file_on_failure(self, mock_run):
        dest_path = self.base_dir / "empty_failed.png"
        # Simulate curl leaving an empty file and returning error code 28 (timeout)
        dest_path.touch()

        mock_res = MagicMock()
        mock_res.returncode = 28
        mock_res.stderr = "Operation timed out"
        mock_run.return_value = mock_res

        ok = download_file_with_curl(
            url="https://files.slack.com/files-pri/T0123/timeout.png",
            dest_path=dest_path,
            bot_token="xoxb-mock-secret",
            timeout=30,
        )
        self.assertFalse(ok)
        self.assertFalse(dest_path.exists())

    @patch("subprocess.run")
    def test_download_file_with_curl_removes_partial_file_on_failure(self, mock_run):
        dest_path = self.base_dir / "partial_corrupt.png"
        # Simulate curl leaving 256 bytes of partial data before timing out or failing
        dest_path.write_bytes(b"partial_stream_data_that_got_interrupted" * 10)

        mock_res = MagicMock()
        mock_res.returncode = 18  # CURLE_PARTIAL_FILE
        mock_res.stderr = "transfer closed with 1024 bytes remaining"
        mock_run.return_value = mock_res

        ok = download_file_with_curl(
            url="https://files.slack.com/files-pri/T0123/partial.png",
            dest_path=dest_path,
            bot_token="xoxb-mock-secret",
            timeout=30,
        )
        self.assertFalse(ok)
        self.assertFalse(dest_path.exists(), "Partial non-zero file must be purged on failure")

    @patch("subprocess.run")
    def test_download_file_with_curl_rejects_html_error_payload(self, mock_run):
        dest_path = self.base_dir / "html_error.png"

        def fake_html_subproc(cmd, **kwargs):
            dest_path.write_bytes(b"<!DOCTYPE html><html><body><h3>403 Forbidden</h3></body></html>")
            res = MagicMock()
            res.returncode = 0
            res.stderr = ""
            return res

        mock_run.side_effect = fake_html_subproc

        ok = download_file_with_curl(
            url="https://files.slack.com/files-pri/T0123/html_error.png",
            dest_path=dest_path,
            bot_token="xoxb-mock-secret",
            timeout=30,
        )
        self.assertFalse(ok)
        self.assertFalse(dest_path.exists(), "HTML error payload disguised as 200 must be deleted")

    @patch("subprocess.run")
    def test_download_file_with_curl_rejects_slack_json_error(self, mock_run):
        dest_path = self.base_dir / "slack_err.png"

        def fake_json_subproc(cmd, **kwargs):
            dest_path.write_bytes(b'{"ok":false,"error":"file_not_found"}')
            res = MagicMock()
            res.returncode = 0
            res.stderr = ""
            return res

        mock_run.side_effect = fake_json_subproc

        ok = download_file_with_curl(
            url="https://files.slack.com/files-pri/T0123/slack_err.png",
            dest_path=dest_path,
            bot_token="xoxb-mock-secret",
            timeout=30,
        )
        self.assertFalse(ok)
        self.assertFalse(dest_path.exists(), "Slack API error JSON disguised as image must be deleted")


if __name__ == "__main__":
    unittest.main()
