"""Tests for the Slack alert helper (H10)."""
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from core.alerts import alert_scrape_run, post_slack


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('boom', request=None, response=None)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.last_url = None
        self.last_payload = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json):
        self.last_url = url
        self.last_payload = json
        return _FakeResponse(200)


class TestPostSlack:
    @override_settings(SLACK_WEBHOOK_URL='')
    def test_no_url_is_no_op(self):
        assert post_slack('hello') is False

    @override_settings(SLACK_WEBHOOK_URL='https://hooks.example/abc')
    def test_posts_to_configured_url(self):
        captured = {}

        class Capture(_FakeClient):
            def post(self, url, json):
                captured['url'] = url
                captured['payload'] = json
                return _FakeResponse(200)

        with patch('core.alerts.httpx.Client', Capture):
            ok = post_slack('Scrape down!')
        assert ok is True
        assert captured['url'] == 'https://hooks.example/abc'
        assert captured['payload'] == {'text': 'Scrape down!'}

    @override_settings(SLACK_WEBHOOK_URL='https://hooks.example/abc')
    def test_http_error_swallowed(self):
        class Failing(_FakeClient):
            def post(self, url, json):
                raise httpx.HTTPError('network down')

        with patch('core.alerts.httpx.Client', Failing):
            assert post_slack('hello') is False  # never raises


class TestAlertScrapeRun:
    @override_settings(SLACK_WEBHOOK_URL='https://hooks.example/abc')
    def test_clean_run_emits_no_alert(self):
        captured = {'called': False}

        class Track(_FakeClient):
            def post(self, url, json):
                captured['called'] = True
                return _FakeResponse(200)

        with patch('core.alerts.httpx.Client', Track):
            alert_scrape_run({'pages_scraped': 10, 'offers_written': 100,
                              'no_offers': 0, 'failures': 0})
        assert captured['called'] is False

    @override_settings(SLACK_WEBHOOK_URL='https://hooks.example/abc')
    def test_no_offers_triggers_alert(self):
        captured = {}

        class Capture(_FakeClient):
            def post(self, url, json):
                captured['payload'] = json
                return _FakeResponse(200)

        with patch('core.alerts.httpx.Client', Capture):
            alert_scrape_run({'pages_scraped': 10, 'offers_written': 50,
                              'no_offers': 3, 'failures': 0})
        assert 'no_offers=3' in captured['payload']['text']
        assert 'failures=0' in captured['payload']['text']

    @override_settings(SLACK_WEBHOOK_URL='https://hooks.example/abc')
    def test_failures_triggers_alert(self):
        captured = {}

        class Capture(_FakeClient):
            def post(self, url, json):
                captured['payload'] = json
                return _FakeResponse(200)

        with patch('core.alerts.httpx.Client', Capture):
            alert_scrape_run({'pages_scraped': 10, 'offers_written': 50,
                              'no_offers': 0, 'failures': 2})
        assert 'failures=2' in captured['payload']['text']
