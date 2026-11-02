"""Loopback-only relay regressions; never use real profiles or proxy credentials."""
import asyncio
import contextlib
import time
import unittest

from fp_proxy import ProxySpec, Relay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.relays = []
        self.handlers = set()
        self.handler_errors = []

    async def asyncTearDown(self):
        for relay in self.relays:
            await asyncio.to_thread(relay._stop)
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.handler_errors, [])

    async def upstream(self, handler, mode='socks5'):
        async def tracked(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await handler(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            except Exception as exc:
                self.handler_errors.append(repr(exc))
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                self.handlers.discard(task)
        server = await asyncio.start_server(tracked, '127.0.0.1', 0)
        self.servers.append(server)
        relay = Relay(ProxySpec(mode=mode, host='127.0.0.1',
                                port=server.sockets[0].getsockname()[1],
                                username='fixture', password='fixture-only'))
        relay._start()
        self.relays.append(relay)
        return relay

    async def socks_client(self, relay):
        reader, writer = await asyncio.open_connection('127.0.0.1', relay._port)
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 2), b'\x05\x00')
        # Domain deliberately stays unresolved and must arrive unchanged upstream.
        writer.write(b'\x05\x01\x00\x03\x0bexample.com\x01\xbb')
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(10), 2)
        return reader, writer, reply

    async def socks_handshake(self, reader, writer, fragmented=False):
        self.assertEqual(await reader.readexactly(3), b'\x05\x01\x02')
        if fragmented:
            writer.write(b'\x05')
            await writer.drain()
            await asyncio.sleep(.03)  # Force two ordinary TCP reads.
            writer.write(b'\x02')
        else:
            writer.write(b'\x05\x02')
        await writer.drain()
        head = await reader.readexactly(2)
        self.assertEqual(head[0], 1)
        self.assertEqual(await reader.readexactly(head[1]), b'fixture')
        n = (await reader.readexactly(1))[0]
        self.assertEqual(await reader.readexactly(n), b'fixture-only')
        writer.write(b'\x01\x00')
        await writer.drain()
        self.assertEqual(await reader.readexactly(18), b'\x05\x01\x00\x03\x0bexample.com\x01\xbb')
        writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6)
        await writer.drain()

    async def test_fragmented_authenticated_socks_reply(self):
        async def proxy(reader, writer):
            await self.socks_handshake(reader, writer, fragmented=True)
            writer.write(await reader.readexactly(4))
            await writer.drain()
        relay = await self.upstream(proxy)
        reader, writer, reply = await self.socks_client(relay)
        try:
            self.assertEqual(reply[1], 0, 'TCP fragmentation must not break proxy authentication')
            writer.write(b'ping')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b'ping')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_reject_unoffered_auth_method(self):
        connected = asyncio.Event()
        async def proxy(reader, writer):
            await reader.readexactly(3)
            writer.write(b'\x05\x00')  # Not offered by authenticated relay.
            await writer.drain()
            if await reader.read(64):
                connected.set()
                writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6)
                await writer.drain()
        relay = await self.upstream(proxy)
        _, writer, reply = await self.socks_client(relay)
        writer.close()
        await writer.wait_closed()
        self.assertNotEqual(reply[1], 0, 'must not silently downgrade configured proxy auth')
        self.assertFalse(connected.is_set())

    async def test_http_half_close_preserves_delayed_response(self):
        payload = bytes(range(256)) * 4096
        async def proxy(reader, writer):
            headers = await reader.readuntil(b'\r\n\r\n')
            self.assertIn(b'Proxy-Authorization: Basic ', headers)
            self.assertNotIn(b'client-secret', headers)
            writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
            await writer.drain()
            self.assertEqual(await reader.read(), b'request-body')
            await asyncio.sleep(.03)
            writer.write(payload)
            await writer.drain()
        relay = await self.upstream(proxy, 'http')
        reader, writer = await asyncio.open_connection('127.0.0.1', relay._port)
        try:
            writer.write(b'CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: client-secret\r\n\r\n')
            await writer.drain()
            self.assertIn(b'200', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2))
            writer.write(b'request-body')
            await writer.drain()
            writer.write_eof()
            result = await asyncio.wait_for(reader.read(), 4)
            self.assertEqual(len(result), len(payload), 'half-close must not truncate reverse traffic')
            self.assertEqual(result, payload)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_concurrent_socks_transfers_keep_bytes_isolated(self):
        async def proxy(reader, writer):
            await self.socks_handshake(reader, writer)
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        relay = await self.upstream(proxy)
        async def transfer(index):
            payload = bytes([index]) * (512 * 1024 + index)
            reader, writer, reply = await self.socks_client(relay)
            self.assertEqual(reply[1], 0)
            async def send():
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
            try:
                _, result = await asyncio.wait_for(asyncio.gather(send(), reader.read()), 10)
                self.assertEqual(result, payload)
            finally:
                writer.close()
                await writer.wait_closed()
        await asyncio.gather(*(transfer(i) for i in range(16)))

    async def test_shutdown_closes_active_tunnels_and_incomplete_handshakes(self):
        async def proxy(reader, writer):
            await self.socks_handshake(reader, writer)
            await reader.read()
        relay = await self.upstream(proxy)
        errors = []
        relay._loop.call_soon_threadsafe(relay._loop.set_exception_handler,
                                         lambda loop, context: errors.append(context['message']))
        reader, writer, reply = await self.socks_client(relay)
        self.assertEqual(reply[1], 0)
        idle_r, idle_w = await asyncio.open_connection('127.0.0.1', relay._port)
        started = time.perf_counter()
        await asyncio.to_thread(relay._stop)
        self.assertLess(time.perf_counter() - started, 3)
        self.assertFalse(relay._thread.is_alive())
        self.assertTrue(relay._loop.is_closed())
        self.assertEqual(relay._port, 0)
        for r, w in [(reader, writer), (idle_r, idle_w)]:
            self.assertEqual(await asyncio.wait_for(r.read(), 1), b'')
            w.close()
            await w.wait_closed()
        self.assertEqual(errors, [], 'no pending tasks may be abandoned on shutdown')
        relay._stop()  # Repeated shutdown is harmless.

    async def test_reject_malformed_socks_connect_reply(self):
        for head in (b'\x04\x00\x00\x01', b'\x05\x00\x01\x01', b'\x05\x00\x00\x09'):
            with self.subTest(head=head.hex()):
                async def proxy(reader, writer):
                    await reader.readexactly(3)
                    writer.write(b'\x05\x02')
                    await writer.drain()
                    await reader.readexactly(2 + len('fixture') + 1 + len('fixture-only'))
                    writer.write(b'\x01\x00')
                    await writer.drain()
                    await reader.readexactly(18)
                    writer.write(head + b'\x00' * 6)
                    await writer.drain()
                relay = await self.upstream(proxy)
                _, writer, reply = await self.socks_client(relay)
                self.assertNotEqual(reply[1], 0)
                writer.close()
                await writer.wait_closed()

    async def test_repeated_start_stop_releases_listener(self):
        async def proxy(reader, writer):
            await self.socks_handshake(reader, writer)
            writer.write(b'ok')
            await writer.drain()
        relay = await self.upstream(proxy)
        for _ in range(20):
            reader, writer, reply = await self.socks_client(relay)
            self.assertEqual(reply[1], 0)
            self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 2), b'ok')
            writer.close()
            await writer.wait_closed()
            port = relay._port
            await asyncio.to_thread(relay._stop)
            self.assertFalse(relay._thread.is_alive())
            with self.assertRaises(OSError):
                await asyncio.open_connection('127.0.0.1', port)
            relay._start()


if __name__ == '__main__':
    unittest.main(verbosity=2)
