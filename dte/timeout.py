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
import logging

root = logging.getLogger()

class Timeout:
    def __init__(self, f, time):
        self.f = f
        self.time = time
        self._active = True
        async def ff():
            await asyncio.sleep(time)
            if self.active:
                try:
                    f()
                except Exception as e:
                    root.error(f'Failed to run the timeout result: {e}')
                self.active = False
        self._task = asyncio.create_task(ff())

    def is_active(self) -> bool:
        return self._active

    def cancel(self) -> bool:
        if self._active:
            self._active = False
            self._task.cancel()
            return True
        return False
