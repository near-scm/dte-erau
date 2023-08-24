# This file is part of DTE-ERAU. Copyright 2023 Embry-Riddle Aeronautical University
#
# DTE-ERAU is free software: you can redistribute it and/or modify it under the terms of the GNU 
# General Public License as published by the Free Software Foundation, either version 3 of the License, or 
# (at your option) any later version.
#
# DTE-ERAU is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without 
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. 
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with DTE-ERAU. If not, see 
# <https://www.gnu.org/licenses/>. 

import aiohttp
import asyncio
import json
import logging
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logging.getLogger('aiohttp.access').setLevel(logging.ERROR)
root = logging.getLogger()
root.debug("Starting...")

routes = web.RouteTableDef()

PDP_URL = 'http://pdp:9990/auth'
WS_UPSTREAM = 'ws://10.142.0.3/ws'

async def check(user: str, tag: str, resource: str) -> bool:
    root.debug(f'Checking for user={user}, tag={tag}, resource={resource}')
    async with aiohttp.ClientSession() as session:
        resp = await session.post(PDP_URL, json={
            "resource_group": tag,
            "resource": resource,
            "user": user
        })
        allowed = resp.status == 200
        status_text = 'allowed' if allowed else '--DENIED--'
        text = f'{status_text}: {user} @ {tag}:{resource}'
        if allowed:
            root.info(text)
        else:
            root.warning(text)
        return allowed

async def is_allowed(user, msg) -> bool:
    if not isinstance(msg, list):
        root.warning('Not a known ws-wire message')
        return False  # This is not a valid message for ws-wire
    mid = msg[0]
    if isinstance(mid, int):
        return True  # This is a response
    elif isinstance(mid, str):
        if mid == '#evt':
            return await check(user, 'wsw-event', msg[2])
        elif mid == '#sub':
            return await check(user, 'wsw-sub', msg[2])
        elif mid in {'#subs', '#error'}:
            return True  # This is protocol structure
        return await check(user, 'wsw-rpc', msg[0])
    return False


@routes.get('/ws')
async def proxy(request):
    ws_head = web.WebSocketResponse()
    await ws_head.prepare(request)

    # Try to find the user
    user = request.headers.get('x-goog-authenticated-user-id')
    if user is None:
        root.warning('User attempted to connect without credentials')
        return web.json_response({'error': 'User identification not provided'}, status=403)

    tail_session = aiohttp.ClientSession()
    async with tail_session.ws_connect(WS_UPSTREAM) as ws_tail:
        # Lets make a connection to the real server
        async def proxy_tail():
            async for msg in ws_tail:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await ws_head.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    root.error(f'Websocket connection closed with exception: {ws_tail.exception()}')
                    return
                    
        task_tail = asyncio.create_task(proxy_tail())

        async def proxy_head():
            async for msg in ws_head:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if await is_allowed(user, data):
                        # root.info('Allowed')
                        await ws_tail.send_str(msg.data)
                    else:
                        # root.warning('Denied')
                        try:
                            resp_id = data[1]
                        except:
                            resp_id = None
                        await ws_tail.send_str(json.dumps(['#error', None, resp_id, 'FORBIDDEN']))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    root.error(f'Websocket connection closed with exception: {ws_head.exception()}')
                    return
        task_head = asyncio.create_task(proxy_head())

        # Wait for one of the tasks to complete
        _done, pending = await asyncio.wait((task_head, task_tail), return_when=asyncio.FIRST_COMPLETED)

    # Then cancel the pending tasks
    for task in pending:
        task.cancel()

    # Return the task for cleanup
    return ws_head

def main():
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, port=9992)

if __name__ == '__main__':
    main()
