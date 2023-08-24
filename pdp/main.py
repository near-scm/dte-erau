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
import json
import logging
import os
import re
import traceback
import yaml
from aiohttp import web
from dataclasses import dataclass
from datetime import datetime, timedelta
from lupa import LuaRuntime
from typing import Callable
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logging.basicConfig(level=logging.INFO)
root = logging.getLogger()
logging.getLogger('aiohttp.access').setLevel(logging.ERROR)
root.debug("Starting...")

EXPIRE_TIME = timedelta(seconds=10)

routes = web.RouteTableDef()


class WatchdogHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
      
    def on_modified(self, event):
        time.sleep(0.1)
        self.callback(event.src_path)

@dataclass
class Policy:
    name: str
    resource_group: str
    resource: str
    groups: set[str]
    keys: list[str]
    function: Callable[[], None]
    code: str

class PolicyEngine:
    DTE_URL_GET = 'http://dte:9991/get'

    def __init__(self, location: str):
        self.log = logging.getLogger('PolicyEngine')
        self.log.debug('Starting...')
        self.location = location
        self._lua = LuaRuntime()
        self._globs = self._lua.globals()
        self.policies = []
        self.group_to_users = {}
        self.user_to_groups = {}
        self.reload()
        self._cached = {}
        self.start_watchdog()

    def start_watchdog(self):
        self._watchdog = Observer()
        watch_dir = os.path.dirname(self.location)
        def look_for_file(fn: str):
            if fn == self.location:
                self.reload()
        self._watchdog.schedule(WatchdogHandler(look_for_file), path=watch_dir)
        self._watchdog.start()

    def reload(self):
        self.log.debug('Reloading...')
        try:
            # Load the yaml definition stored as a file
            with open(self.location) as f:
                data = yaml.safe_load(f)

            # Load groups and users going in either direction
            group_to_users = data['groups']
            user_to_groups = {}
            for group, users in group_to_users.items():
                for user in users:
                    user_to_groups.setdefault(user, set()).add(group)

            # Load the policies
            policies = list(map(self.create_policy, data['policies']))

        except Exception as e:
            # Something failed, so we will not be applying the change
            self.log.error('Could not reload the policy engine')
            self.log.error(traceback.format_exc())
        else:
            # Success, apply the changes
            self.policies = policies
            self.group_to_users = group_to_users
            self.user_to_groups = user_to_groups
            self.log.info('Reloaded policy!')
            self.log.debug(f'Groups: {group_to_users}')
            self.log.debug(f'Users: {user_to_groups}')
            self.log.debug(f'Policies: {policies}')

    def create_policy(self, policy) -> Policy:
        rg = policy['resource_group']
        r = policy['resource']
        function = policy['policy'].strip().split('\n', -1)
        function[-1] = f'return {function[-1]}'
        function = '\n'.join(function)
        return Policy(
            name=f'{rg}:{r}',
            resource_group=rg,
            resource=re.compile(r),
            groups=set(policy['groups']),
            keys=policy['keys'],
            function=self._lua.compile(function),
            code=function
        )

    async def eval_cached(self, user: str, resource_group: str, resource: str) -> bool:
        key = (user, resource_group, resource)
        got = self._cached.get(key, None)
        now = datetime.now()
        if got is not None:
            cache, time = got
            dt = now - time
            if dt < EXPIRE_TIME:
                return cache
        got = await self.eval(*key)
        self._cached[key] = (got, now)
        return got

    async def eval(self, user: str, resource_group: str, resource: str) -> bool:
        groups = self.user_to_groups.get(user)
        if groups is None:
            self.log.warning(f'The user {user} is not recognized by any group')
            return False  # Does not belong to any group that has permissions / not a registered user
        for policy in self.policies:
            if resource_group != policy.resource_group:
                continue  # Does not apply to this resource group
            if policy.resource.match(resource) is None:
                continue  # Does not apply to this resource
            if not (policy.groups & groups):
                continue  # Does not apply to the groups this user is in

            # This actually applies to us:
            self.log.debug(f'Apply policy: {policy.name}')
            # Lets see what we need from DTE
            if policy.keys:
                self.log.debug(f'Fetching {", ".join(policy.keys)} from DTE')
                async with aiohttp.ClientSession() as session:
                    response = await session.get(self.DTE_URL_GET, json={
                        'user': user,
                        'keys': policy.keys
                    })
                    dte_vars = await response.json()
            else:
                # This policy does not use trust score values
                dte_vars = {}
            # Wipe our runtime globals
            for k in self._globs.keys():
                del self._globs[k]
            self._globs['dte'] = dte_vars
            # Run the function
            try:
                result = bool(policy.function())
            except Exception as e:
                self.log.warning(f'Error trying to apply policy ({policy.name}): {e}')
                result = False
            else:
                if not result:
                    self.log.warning(f'Policy {policy.name}:\n{policy.code}\nVARS = {json.dumps(dte_vars, indent=4)}')
            status_text = 'allowed' if result else 'denied'
            self.log.debug(f'{status_text}: {user} accessing {resource_group}:{resource} by the policy {policy.name}')
            return result

        self.log.warning(f'--DENY-- {user} accessing {resource_group}:{resource} got default denied due to fall-through case')
        return False # Default deny run-through case

pe = PolicyEngine('./policy.yaml')

@routes.post('/auth')
async def hello(request):
    body = await request.json()
    user = body['user']
    rg = body['resource_group']
    r = body['resource']
    result = await pe.eval_cached(user, rg, r)
    status_text = 'allowed' if result else '--DENIED--'
    root.info(f'{status_text}: {user} accessing {rg}:{r}')
    status = 200 if result else 403
    return web.Response(text="", status=status)

def main():
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, port=9990)

if __name__ == '__main__':
    main()
