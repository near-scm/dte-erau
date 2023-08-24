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

import asyncio
import base64
import logging
import re
import time
import yaml
from aiohttp import web
from datetime import datetime, timedelta
from lupa import LuaRuntime
from typing import Iterable
from watchdog.events import FileSystemEventHandler

logging.basicConfig(level=logging.INFO)
root = logging.getLogger()
logging.getLogger('aiohttp.access').setLevel(logging.ERROR)
root.debug("Starting...")

routes = web.RouteTableDef()


class WatchdogHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
      
    def on_modified(self, event):
        time.sleep(0.1)
        self.callback(event.src_path)

"""
A TScP for local testing
"""
class FileTScP:
    def __init__(self, name: str, location: str):
        self.log = logging.getLogger(f'TScP {name}')
        self.log.info(f'Starting TScP ({location})')
        self.name = name
        self.location = location
        self.reload()

    def reload(self):
        # self.log.debug('Reloading')
        with open(self.location) as f:
            scores = yaml.safe_load(f)
        # We need to ensure that all the usernames and keys are strings and all the values are floats
        self.scores = {str(user): {str(k): float(v) for k, v in vals.items()} for user, vals in scores.items()}
        self.log.debug(f'{self.name} is loading with {self.scores}')


class NetworkTScP:
    def __init__(self, data):
        self.name = name = data['name']
        self.pull_endpoint = data.get('pull')
        self.log = logging.getLogger(f'TScP {name}')
        self.log.info(f'Starting Networked TScP')
        self.scores = {}  # type: dict[str, dict[str, float]]
        self.timers = {}  # type: dict[tuple[str, str], datetime]
        self._lua = LuaRuntime()
        self._globs = self._lua.globals()
        # Times
        self.old = timedelta(hours=1)
        self.stale = timedelta(hours=2)

    async def gardener(self):
        while True:
            await asyncio.sleep(10)
            now = datetime.now()
            rm = set()
            old = []
            for k, v in self.timers.items():
                dt = v - now
                if dt >= self.stale:
                    k1, k2 = k
                    self.scores[k1].pop(k2)
                    rm.add(k)
                elif dt >= self.old:
                    old.append(k)
            for k in rm:
                self.timers.pop(k)

    def update(self, data) -> None:
        """
        Take in the scores for all the users and update the scores and expiration timers accordingly
        """
        now = datetime.now()
        mapping = None
        if 'mapping' in data:
            mapping = {}
            for k, v in data['mapping'].items():
                v = v.strip()
                if v[:2] == '##':
                    v = base64.b64decode(v[2:].encode()).decode()
                function = v.split('\n', -1)
                function[-1] = f'return {function[-1]}'
                function = '\n'.join(function)
                mapping[k] = self._lua.compile(function)

        for user, user_scores in data['scores'].items():
            if mapping:
                normalized_scores = {}

                # Go through the entire mapping table one at a time:
                for mapping_key, function in mapping.items():
                    # Wipe our lua runtime globals
                    for k in self._globs.keys():
                        del self._globs[k]

                    # Inject the internal scoring into the lua interpreter
                    self._globs['_score'] = data['scores']
                    for k, v in user_scores.items():
                        if re.match(r'\A[a-z][a-z0-9_]*\Z', k, flags=re.IGNORECASE):
                            self._globs[k] = v
                    
                    # Run the function
                    try:
                        result = function()
                    except Exception as e:
                        self.log.error(f'Failed to perform mapping function for {k} on {user}: {e}')
                        continue

                    # Interpret the response
                    if result is not None:
                        try:
                            normalized_scores[mapping_key] = float(result)
                        except ValueError:
                            self.log.error(f'The returned value for the policy "{k}" needs to be a nil response or a type convertible to a float. Got: {type(result)}.')
            else:
                normalized_scores = user_scores

            # Update the timers to the new values to be now
            self.timers.setdefault(user, {}).update({k: now for k in normalized_scores.keys()})

            # Update the scores for this user
            self.scores.setdefault(user, {}).update(normalized_scores)

class DTE:
    def __init__(self):
        self.log = logging.getLogger('DTE')
        self.log.debug('Starting DTE')
        self.tscps = {}

    def tscp_changed(self, path):
        for tscp in self.tscps.values():
            if path.endswith(tscp.location):
                tscp.reload()
                return

    def get(self, user: str, keys: Iterable[str]) -> dict[str, float]:
        out = {}
        for key in keys:
            tscp_name, score_key = key.split(':', 1)
            tscp = self.tscps.get(tscp_name)
            if not tscp:
                continue
            out[key] = tscp.scores.get(user, {}).get(score_key)
        return out

    def onramp(self, tscp) -> None:
        name = tscp['name']
        if name in self.tscps:
            self.tscps.pull = tscp.get('pull')
        else:
            new_tscp = NetworkTScP(tscp)
            self.tscps[new_tscp.name] = new_tscp

    def update(self, data) -> None:
        name = data['name']
        if name not in self.tscps:
            self.onramp({'name': name, 'pull': None})
        self.tscps[name].update(data)

dte = DTE()

@routes.get('/get')
async def get_trust_scores(request):
    try:
        body = await request.json()
        user = body['user']
        keys = body['keys']
    except:
        raise web.HTTPBadRequest()
    got = dte.get(user, keys)
    return web.json_response(got)

@routes.get('/onramp')
async def onramp(request):
    root.info('On-ramping TScP')
    body = await request.json()
    dte.onramp(body)
    return web.json_response(True)

@routes.post('/update')
async def update_trust_scores(request):
    root.info('Got update from tscp')
    body = await request.json()
    dte.update(body)
    return web.json_response(True)

app = web.Application()
app.add_routes(routes)
web.run_app(app, port=9991)

