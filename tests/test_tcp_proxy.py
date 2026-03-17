'''Tests for the TCP pass-through relay (MQTT, camera).

Uses real loopback TCP connections to exercise actual byte-piping.
The proxy is a simple bidirectional relay with no protocol awareness.
'''

from __future__ import annotations

import asyncio

import pytest

from src.tcp_proxy import _handle, _log_task_exception, start_tcp_proxy


class TestRelay:
  @pytest.mark.asyncio
  async def test_data_relayed_to_target(self):
    '''Client data arrives at the target server through the proxy.'''
    received = asyncio.Event()
    received_data = bytearray()

    async def capture_handler(reader, writer):
      data = await reader.read(4096)
      received_data.extend(data)
      received.set()
      writer.close()
      await writer.wait_closed()

    target = await asyncio.start_server(capture_handler, '127.0.0.1', 0)
    target_port = target.sockets[0].getsockname()[1]
    proxy = await start_tcp_proxy(0, '127.0.0.1', target_port, 'test')
    proxy_port = proxy.sockets[0].getsockname()[1]

    try:
      _, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
      writer.write(b'hello target')
      await writer.drain()

      await asyncio.wait_for(received.wait(), timeout=2.0)
      assert bytes(received_data) == b'hello target'
      writer.close()
    finally:
      proxy.close()
      target.close()
      await proxy.wait_closed()
      await target.wait_closed()

  @pytest.mark.asyncio
  async def test_response_relayed_to_client(self):
    '''Target server responses arrive back at the client through the proxy.'''

    async def greet_handler(reader, writer):
      await reader.read(4096)  # wait for client to send something
      writer.write(b'welcome!')
      await writer.drain()
      writer.close()
      await writer.wait_closed()

    target = await asyncio.start_server(greet_handler, '127.0.0.1', 0)
    target_port = target.sockets[0].getsockname()[1]
    proxy = await start_tcp_proxy(0, '127.0.0.1', target_port, 'test')
    proxy_port = proxy.sockets[0].getsockname()[1]

    try:
      reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
      writer.write(b'hi')
      await writer.drain()

      response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
      assert response == b'welcome!'
      writer.close()
    finally:
      proxy.close()
      target.close()
      await proxy.wait_closed()
      await target.wait_closed()

  @pytest.mark.asyncio
  async def test_large_transfer_beyond_buffer_size(self):
    '''Transfers >64KB (the internal buffer) are relayed intact.'''
    payload = b'X' * 200_000

    async def sink_handler(reader, writer):
      await reader.read(4096)
      writer.write(payload)
      await writer.drain()
      writer.close()
      await writer.wait_closed()

    target = await asyncio.start_server(sink_handler, '127.0.0.1', 0)
    target_port = target.sockets[0].getsockname()[1]
    proxy = await start_tcp_proxy(0, '127.0.0.1', target_port, 'test')
    proxy_port = proxy.sockets[0].getsockname()[1]

    try:
      reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
      writer.write(b'go')
      await writer.drain()

      received = b''
      while True:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        if not chunk:
          break
        received += chunk
      assert received == payload
      writer.close()
    finally:
      proxy.close()
      target.close()
      await proxy.wait_closed()
      await target.wait_closed()

  @pytest.mark.asyncio
  async def test_target_disconnect_propagates_to_client(self):
    '''When the target drops, the client sees EOF.'''

    async def close_immediately(reader, writer):
      writer.close()
      await writer.wait_closed()

    target = await asyncio.start_server(close_immediately, '127.0.0.1', 0)
    target_port = target.sockets[0].getsockname()[1]
    proxy = await start_tcp_proxy(0, '127.0.0.1', target_port, 'test')
    proxy_port = proxy.sockets[0].getsockname()[1]

    try:
      reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
      data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
      assert data == b''
      writer.close()
    finally:
      proxy.close()
      target.close()
      await proxy.wait_closed()
      await target.wait_closed()


class TestUnreachableTarget:
  @pytest.mark.asyncio
  async def test_unreachable_target_closes_client(self):
    '''When the target refuses connection, the client is disconnected.'''
    accepted = asyncio.Event()
    client_streams = {}

    async def grab_client(reader, writer):
      client_streams['reader'] = reader
      client_streams['writer'] = writer
      accepted.set()

    server = await asyncio.start_server(grab_client, '127.0.0.1', 0)
    server_port = server.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection('127.0.0.1', server_port)
    await asyncio.wait_for(accepted.wait(), timeout=2.0)

    await asyncio.wait_for(
      _handle(
        client_streams['reader'],
        client_streams['writer'],
        '127.0.0.1',
        1,
        'test',
      ),
      timeout=5.0,
    )

    writer.close()
    server.close()
    await server.wait_closed()


class TestTaskExceptionLogging:
  '''Tests for _log_task_exception done callback (fire-and-forget task handling).'''

  @pytest.mark.asyncio
  async def test_exception_in_task_is_logged(self, caplog):
    '''Unhandled exceptions in the proxy task are logged, not silently dropped.'''

    async def raise_error():
      raise RuntimeError('test boom')

    task = asyncio.create_task(raise_error(), name='test-proxy')
    task.add_done_callback(_log_task_exception)
    with pytest.raises(RuntimeError, match='test boom'):
      await task

    assert 'test boom' in caplog.text
    assert 'RuntimeError' in caplog.text

  @pytest.mark.asyncio
  async def test_cancelled_task_not_logged_as_exception(self, caplog):
    '''CancelledError is swallowed; no exception is logged.'''

    async def slow():
      await asyncio.sleep(10)

    task = asyncio.create_task(slow(), name='test-proxy')
    task.add_done_callback(_log_task_exception)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task

    assert 'CancelledError' not in caplog.text
    assert 'Unhandled exception' not in caplog.text

  @pytest.mark.asyncio
  async def test_successful_task_logs_nothing(self, caplog):
    '''Normal completion does not raise or log.'''

    async def succeed():
      pass

    task = asyncio.create_task(succeed(), name='test-proxy')
    task.add_done_callback(_log_task_exception)
    await task

    assert 'Unhandled exception' not in caplog.text
