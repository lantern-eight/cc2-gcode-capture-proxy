'''CC2 G-Code Capture Proxy: entry point.

Starts four services:
  • HTTP reverse-proxy   (:80)   - intercepts PUT /upload, saves G-code copy
  • MQTT TCP relay        (:1883) - transparent pass-through
  • MQTT-WS TCP relay     (:9001) - transparent pass-through (WebSocket)
  • Camera TCP relay      (:8080) - transparent pass-through

Also exposes a REST API on the same HTTP port for querying
captured G-code metadata (GET /api/filament, /api/health).
'''

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiohttp import web

from .api import API
from .config import Config
from .http_proxy import HTTPProxy
from .storage import GCodeStorage
from .tcp_proxy import start_tcp_proxy

logger = logging.getLogger('cc2_proxy')


def _setup_logging(level: str) -> None:
  logging.basicConfig(
    level=getattr(logging, level.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
  )


async def _run() -> None:
  config = Config()
  _setup_logging(config.log_level)

  if not config.printer_ip or not config.printer_ip.strip():
    raise SystemExit(
      'PRINTER_IP is required but not set. '
      "Set the PRINTER_IP environment variable to your printer's IP address."
    )

  logger.info('CC2 G-Code Capture Proxy starting')
  logger.info('  Printer IP  : %s', config.printer_ip)
  logger.info('  G-code dir  : %s', config.gcode_dir)
  logger.info('  Retention   : %d days', config.retention_days)
  logger.info('  Timezone    : %s', config.gcode_timezone.key)
  logger.info('  Store gcode : %s', config.store_gcode)

  storage = GCodeStorage(
    config.gcode_dir,
    config.retention_days,
    store_gcode=config.store_gcode,
    tz=config.gcode_timezone,
  )

  removed = storage.cleanup_old_files()
  if removed:
    logger.info('Startup cleanup removed %d expired file(s)', removed)

  # --- HTTP proxy (smart: captures PUT /upload) ---
  http_proxy = HTTPProxy(config, storage)
  await http_proxy.start()

  app = web.Application(client_max_size=config.max_body_size)
  api = API(storage)
  api.register_routes(app)
  app.router.add_route('*', '/{path_info:.*}', http_proxy.handle_request)

  runner = web.AppRunner(app, access_log=None)
  await runner.setup()
  site = web.TCPSite(runner, '0.0.0.0', config.http_port)
  await site.start()
  logger.info('HTTP  proxy listening on :%d', config.http_port)

  # --- TCP relays (dumb pass-through) ---
  mqtt_srv = await start_tcp_proxy(
    config.mqtt_port,
    config.printer_ip,
    1883,
    'MQTT',
  )
  camera_srv = await start_tcp_proxy(
    config.camera_port,
    config.printer_ip,
    8080,
    'Camera',
  )
  mqtt_ws_srv = await start_tcp_proxy(
    config.mqtt_ws_port,
    config.printer_ip,
    9001,
    'MQTT-WS',
  )

  # --- background maintenance ---
  bg_tasks = [
    asyncio.create_task(storage.periodic_cleanup()),
    asyncio.create_task(http_proxy.cleanup_stale_sessions()),
  ]

  logger.info('All services started — proxying to %s, API at /api/', config.printer_ip)

  # --- wait for shutdown signal ---
  stop = asyncio.Event()
  loop = asyncio.get_running_loop()
  for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, stop.set)
  await stop.wait()

  # --- teardown ---
  logger.info('Shutting down…')
  for task in bg_tasks:
    task.cancel()
  mqtt_srv.close()
  camera_srv.close()
  mqtt_ws_srv.close()
  await asyncio.gather(
    mqtt_srv.wait_closed(),
    camera_srv.wait_closed(),
    mqtt_ws_srv.wait_closed(),
    *bg_tasks,
    return_exceptions=True,
  )
  await http_proxy.stop()
  await runner.cleanup()
  logger.info('Shutdown complete')


def main() -> None:
  with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(_run())


if __name__ == '__main__':
  main()
