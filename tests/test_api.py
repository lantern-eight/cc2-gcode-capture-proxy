'''Tests for the REST API: filament lookup, latest metadata, health check.

Uses aiohttp test client against a real app with a real storage backend
(pytest tmp_path).  No mocks needed — the API is thin handlers over storage.
'''

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api import API
from src.storage import GCodeStorage
from tests.conftest import make_gcode

_UTC = ZoneInfo('UTC')


def _make_app(storage: GCodeStorage) -> web.Application:
  app = web.Application()
  api = API(storage)
  api.register_routes(app)
  return app


@pytest.fixture
def api_storage(tmp_path):
  return GCodeStorage(
    str(tmp_path / 'gcode'),
    retention_days=90,
    store_gcode=False,
    tz=_UTC,
  )


# ===================================================================
# GET /api/health
# ===================================================================


class TestHealth:
  @pytest.mark.asyncio
  async def test_returns_ok(self, api_storage):
    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get('/api/health')
      assert response.status == 200
      body = await response.json()
      assert body == {'status': 'ok'}


# ===================================================================
# GET /api/filament?filename=...
# ===================================================================


class TestFilamentLookup:
  @pytest.mark.asyncio
  async def test_returns_metadata_for_known_file(self, api_storage):
    data = make_gcode(
      input_filename_base='benchy',
      total_grams=2.76,
      per_slot_grams='0.0, 0.0, 0.0, 2.76',
    )
    api_storage.save_gcode(data)

    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get('/api/filament', params={'filename': 'benchy.gcode'})
      assert response.status == 200
      body = await response.json()
      assert body['filename'] == 'benchy.gcode'
      assert body['filament']['total_grams'] == pytest.approx(2.76)
      assert body['filament']['per_slot_grams'] == pytest.approx([0.0, 0.0, 0.0, 2.76])

  @pytest.mark.asyncio
  async def test_returns_404_for_unknown_file(self, api_storage):
    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get(
        '/api/filament', params={'filename': 'nonexistent.gcode'}
      )
      assert response.status == 404
      body = await response.json()
      assert 'error' in body

  @pytest.mark.asyncio
  async def test_returns_400_when_filename_missing(self, api_storage):
    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get('/api/filament')
      assert response.status == 400
      body = await response.json()
      assert 'error' in body


# ===================================================================
# GET /api/filament/latest
# ===================================================================


class TestFilamentLatest:
  @pytest.mark.asyncio
  async def test_returns_most_recent(self, api_storage):
    data_old = make_gcode(input_filename_base='first', total_grams=1.0)
    api_storage.save_gcode(data_old)

    data_new = make_gcode(input_filename_base='second', total_grams=5.0)
    api_storage.save_gcode(data_new)

    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get('/api/filament/latest')
      assert response.status == 200
      body = await response.json()
      assert body['filament']['total_grams'] == pytest.approx(5.0)

  @pytest.mark.asyncio
  async def test_returns_404_on_empty_archive(self, api_storage):
    app = _make_app(api_storage)
    async with TestClient(TestServer(app)) as client:
      response = await client.get('/api/filament/latest')
      assert response.status == 404
      body = await response.json()
      assert 'error' in body
