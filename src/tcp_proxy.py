'''Network proxies for the CC2 printer.

TCP pass-through for MQTT (1883), MQTT-WS (9001), and MJPEG camera (8080).
'''

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_BUF = 65_536  # 64 KiB read buffer


def _log_task_exception(task: asyncio.Task) -> None:
  '''Done callback: log any unhandled exception so it does not become a silent warning.'''
  try:
    task.result()
  except asyncio.CancelledError:
    # Cancellation is normal control flow, not a bug. Logging it would be noisy
    # and misleading during shutdown when many tasks are cancelled at once.
    pass
  except Exception:
    logger.exception('Unhandled exception in %s proxy task', task.get_name())


async def start_tcp_proxy(
  listen_port: int,
  target_host: str,
  target_port: int,
  label: str = 'TCP',
) -> asyncio.Server:
  '''Bind *listen_port* and relay every connection to *target_host:target_port*.'''

  async def _on_connect(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
  ) -> None:
    task = asyncio.create_task(
      _handle(client_reader, client_writer, target_host, target_port, label),
      name=f'{label}-proxy',
    )
    task.add_done_callback(_log_task_exception)

  server = await asyncio.start_server(_on_connect, '0.0.0.0', listen_port)
  logger.info(
    '%s proxy listening on :%d → %s:%d',
    label,
    listen_port,
    target_host,
    target_port,
  )
  return server


# ------------------------------------------------------------------
# Internal
# ------------------------------------------------------------------


async def _handle(
  client_reader: asyncio.StreamReader,
  client_writer: asyncio.StreamWriter,
  host: str,
  port: int,
  label: str,
) -> None:
  peer = client_writer.get_extra_info('peername')
  logger.debug('%s: new connection from %s', label, peer)

  try:
    server_reader, server_writer = await asyncio.open_connection(host, port)
  except (ConnectionRefusedError, OSError) as exception:
    logger.warning('%s: cannot reach %s:%d - %s', label, host, port, exception)
    client_writer.close()
    await client_writer.wait_closed()
    return

  logger.info('%s: proxying %s ↔ %s:%d', label, peer, host, port)
  await asyncio.gather(
    _pipe(client_reader, server_writer, f'{label} client→server'),
    _pipe(server_reader, client_writer, f'{label} server→client'),
  )
  logger.debug('%s: connection from %s closed', label, peer)


async def _pipe(
  reader: asyncio.StreamReader,
  writer: asyncio.StreamWriter,
  tag: str,
) -> None:
  try:
    while True:
      data = await reader.read(_BUF)
      if not data:
        break
      writer.write(data)
      await writer.drain()
  except (ConnectionResetError, BrokenPipeError, ConnectionError):
    pass
  except Exception:
    logger.exception('Error in %s', tag)
  finally:
    try:
      writer.close()
      await writer.wait_closed()
    except Exception:
      pass
