# -*- coding: utf-8 -*-

"""
MIT License

Copyright (c) 2024 Oli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import asyncio
import aiohttp
import json
import functools

from .message import FriendMessage, PartyMessage

from aiohttp import hdrs, helpers, client_reqrep, connector
from aiohttp.http import StreamWriter, HttpVersion10, HttpVersion11


class WebsocketRequest(aiohttp.client_reqrep.ClientRequest):
    async def send(self,
                   conn: "aiohttp.connector.Connection"
                   ) -> "aiohttp.ClientResponse":
        if self.method == hdrs.METH_CONNECT:
            connect_host = self.url.raw_host
            assert connect_host is not None
            if helpers.is_ipv6_address(connect_host):
                connect_host = f"[{connect_host}]"
            path = f"{connect_host}:{self.url.port}"
        elif self.proxy and not self.is_ssl():
            path = str(self.url)
        else:
            path = self.url.raw_path
            if self.url.raw_query_string:
                path += "?" + self.url.raw_query_string

        protocol = conn.protocol
        assert protocol is not None
        writer = StreamWriter(
            protocol,
            self.loop,
            on_chunk_sent=functools.partial(
                self._on_chunk_request_sent, self.method, self.url
            ),
            on_headers_sent=functools.partial(
                self._on_headers_request_sent, self.method, self.url
            ),
        )

        if self.compress:
            writer.enable_compression(self.compress)

        if self.chunked is not None:
            writer.enable_chunking()

        if (
            self.method in self.POST_METHODS
            and hdrs.CONTENT_TYPE not in self.skip_auto_headers
            and hdrs.CONTENT_TYPE not in self.headers
        ):
            self.headers[hdrs.CONTENT_TYPE] = "application/octet-stream"

        connection = self.headers.get(hdrs.CONNECTION)
        if not connection:
            if self.keep_alive():
                if self.version == HttpVersion10:
                    connection = "keep-alive"
            else:
                if self.version == HttpVersion11:
                    connection = "close"

        if connection is not None:
            self.headers[hdrs.CONNECTION] = connection

        status_line = "{0} {1} HTTP/{2[0]}.{2[1]}".format(
            self.method, path, self.version
        ) if "/stomp" not in path else "GET https://connect.epicgames.dev/ " \
                                       "HTTP/1.1"
        await writer.write_headers(status_line, self.headers)

        self._writer = self.loop.create_task(self.write_bytes(writer, conn))

        response_class = self.response_class
        assert response_class is not None
        self.response = response_class(
            self.method,
            self.original_url,
            writer=self._writer,
            continue100=self._continue,
            timer=self._timer,
            request_info=self.request_info,
            traces=self._traces,
            loop=self.loop,
            session=self._session,
        )
        return self.response


class WebsocketClient:
    def __init__(self, client) -> None:
        self.client = client

        self.wss_session = None
        self.websocket = None

    async def set_session(self) -> None:
        self.wss_session = aiohttp.ClientSession(
            skip_auto_headers=["Accept", "Accept-Encoding", "User-Agent"],
            request_class=WebsocketRequest
        )

    async def send_presence(self, connection_id: str) -> None:
        await self.client.http.chat_send_presence(
            connection_id=connection_id,
            auth=f'bearer {self.client.auth.chat_access_token}'
        )

    async def send_heartbeat(self) -> None:
        while True:
            await self.websocket.send_str("\n")
            await asyncio.sleep(45)

    async def parse_message(self, raw: str) -> None:
        raw_headers, raw_json = raw.split('\n\n', 1)
        header_lines = raw_headers.splitlines()
        message_type = header_lines[0]

        headers = {}
        for line in header_lines[1:]:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()

        data = json.loads(raw_json[:-1])

        if data['type'] == 'social.chat.v1.NEW_WHISPER':
            author = self.client.get_friend(
                data['payload']['message']['senderId']
            )
            if author is None:
                try:
                    author = await self.client.wait_for(
                        'friend_add',
                        check=lambda f: f.id == data['payload']['message']
                        ['senderId'],
                        timeout=2
                    )
                except asyncio.TimeoutError:
                    return

            try:
                m = FriendMessage(
                    client=self.client,
                    author=author,
                    content=data['payload']['message']['body']
                )
                self.client.dispatch_event('friend_message', m)
            except ValueError:
                pass
        elif data['type'] == 'social.chat.v1.NEW_MESSAGE':
            user_id = data['payload']['message']['senderId']
            party = self.client.party

            if (user_id == self.client.user.id
                    or user_id not in party._members):
                return

            self.client.dispatch_event('party_message', PartyMessage(
                client=self.client,
                party=party,
                author=party._members[data['payload']['message']['senderId']],
                content=data['payload']['message']['body']
            ))

    async def connect_to_websocket(self) -> None:
        headers = {
            'Authorization': f'Bearer {self.client.auth.chat_access_token}',
            'Epic-Connect-Protocol': 'stomp',
            "Sec-WebSocket-Protocol": "v10.stomp,v11.stomp,v12.stomp",
            'Epic-Connect-Device-Id': " ",
            "Pragma": "no-cache",
            "Connection": "Upgrade",
            "Cache-Control": "no-cache",
            "Host": "connect.epicgames.dev",
            "Origin": "http://connect.epicgames.dev",
        }
        async with self.wss_session.ws_connect(
            "wss://connect.epicgames.dev/stomp",
            protocols=['stomp'],
            headers=headers
        ) as websocket:
            self.websocket = websocket
            connect_frame = f"CONNECT\nheart-beat:30000,0\n" \
                            f"accept-version:1.0,1.1,1.2\n\n\x00"
            await websocket.send_str(connect_frame)

            heartbeat_message = await websocket.receive()
            self.client.loop.create_task(self.send_heartbeat())

            await websocket.send_str(f"SUBSCRIBE\nid:0\n"
                                     f"destination:launcher\n\n\x00")
            raw_connection_id = await websocket.receive()

            await self.send_presence(
                connection_id=(raw_connection_id.data.decode())
                .split('"connectionId":"')[1].split('"')[0]
            )
            async for msg in websocket:
                await self.parse_message(msg.data.decode())

    async def run(self) -> None:
        await self.set_session()
        self.client.loop.create_task(self.connect_to_websocket())
