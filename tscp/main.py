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
import os
import time
import yaml
from aiohttp import web
from typing import Dict
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


DTE_URL = 'http://dte:9991'
DTE_URL_TS_UPDATE = f'{DTE_URL}/update'
DTE_URL_ONRAMP = f'{DTE_URL}/onramp'

class WatchdogHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
      
    def on_modified(self, event):
        time.sleep(0.1)
        self.callback(event.src_path)

class FileTScP:
    mapping: Dict[str, str]

    def __init__(self, name: str, location: str):
        self.log = logging.getLogger(f'TScP {name}')
        self.log.info(f'Starting TScP ({location})')
        self.name = name
        self.location = location
        self.mapping = None
        self.reload()
        self.start_watchdog()

    def start_task(self):
        self._loop_task = asyncio.create_task(self._trust_score_loop())

    async def _trust_score_loop(self) -> None:
        while True:
            await self.send_trust_scores()
            await asyncio.sleep(60)

    async def send_trust_scores(self):
        self.log.info("Sending recent trust scores")
        out = {
            'name': self.name,
            'scores': self.scores,
        }
        if self.mapping:
            out['mapping'] = self.mapping
        async with aiohttp.ClientSession() as session:
            _response = await session.post(DTE_URL_TS_UPDATE, json=out)

    def start_watchdog(self):
        self._watchdog = Observer()
        watch_dir = os.path.dirname(self.location)
        def look_for_file(fn: str):
            if fn == self.location:
                self.reload()
            else:
                self.log.debug(f'{fn} != {self.location}')
        self._watchdog.schedule(WatchdogHandler(look_for_file), path=watch_dir)
        self._watchdog.start()

    def reload(self):
        self.log.info('Reloading!')
        with open(self.location) as f:
            data = yaml.safe_load(f)
        # We need to ensure that all the usernames and keys are strings and all the values are floats
        self.mapping = data.get('mapping')
        self.scores = {str(user): {str(k): float(v) for k, v in vals.items()} for user, vals in data['scores'].items()}
        self.log.debug(f'{self.name} is loading with {self.scores}')

    async def send_onramp(self):
        async with aiohttp.ClientSession() as session:
            response = await session.post(DTE_URL_ONRAMP, json={
                'name': self.name,
                'pull': None  # Optional: This is url that the DTE can contact us to get up-to-date trust score information
            })

    def get(self, user, scores) -> dict[str, float]:
        score_table = self.scores[user]
        return {k: score_table[k] for k in scores}

def main():
    logging.basicConfig(level=logging.DEBUG)
    root = logging.getLogger()
    root.debug("Starting...")

    name = os.environ.get('NAME')
    file = os.environ.get('FILE')
    tscp = FileTScP(name, file)

    routes = web.RouteTableDef()

    @routes.get('/get')
    async def get(request):
        body = await request.json()
        user = body['user']
        keys = body['keys']
        got = tscp.get(user, keys)
        root.info(f'TScP -> {user}, {keys} -> {json.dumps(got, indent=4)}')
        return web.json_response(got)


    async def start():
        await asyncio.sleep(2)  # Wait 2 seconds for DTE to start
        tscp.start_task()

        # Start the application
        app = web.Application()
        app.add_routes(routes)
        port = os.environ.get('PORT', 9993)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()

        # Now that we have started up the server, lets onramp ourselves to DTE
        await tscp.send_onramp()

        # Make sure the event loop is left open
        while True:
            await asyncio.sleep(3600)
    asyncio.run(start())

if __name__ == '__main__':
    main()
