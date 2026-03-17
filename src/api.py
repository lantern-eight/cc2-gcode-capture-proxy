'''REST API for querying captured G-code metadata.

Endpoints:
  GET /api/filament?filename=CC2_benchy.gcode  → JSON metadata
  GET /api/filament/latest                     → most recent capture
  GET /api/health                              → {"status": "ok"}
'''

from __future__ import annotations

import logging

from aiohttp import web

from .storage import GCodeStorage

logger = logging.getLogger(__name__)


class API:
  def __init__(self, storage: GCodeStorage) -> None:
    self._storage = storage

  def register_routes(self, app: web.Application) -> None:
    '''Add API routes.  Call *before* the catch-all proxy route.'''
    app.router.add_get('/api/health', self._handle_health)
    app.router.add_get('/api/filament/latest', self._handle_filament_latest)
    app.router.add_get('/api/filament', self._handle_filament)

  # ------------------------------------------------------------------
  # Handlers
  # ------------------------------------------------------------------

  async def _handle_health(self, _request: web.Request) -> web.Response:
    return web.json_response({'status': 'ok'})

  async def _handle_filament(self, request: web.Request) -> web.Response:
    filename = request.query.get('filename')
    if not filename:
      return web.json_response(
        {'error': 'missing required query parameter: filename'},
        status=400,
      )

    metadata = self._storage.find_metadata(filename)
    if metadata is None:
      return web.json_response(
        {'error': f'no metadata found for filename: {filename}'},
        status=404,
      )
    return web.json_response(metadata)

  async def _handle_filament_latest(self, _request: web.Request) -> web.Response:
    metadata = self._storage.get_latest_metadata()
    if metadata is None:
      return web.json_response(
        {'error': 'no metadata available'},
        status=404,
      )
    return web.json_response(metadata)
