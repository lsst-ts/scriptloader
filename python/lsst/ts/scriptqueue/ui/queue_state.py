# This file is part of ts_scriptqueue.
#
# Developed for the LSST Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = ["QueueState"]

import logging

from lsst.ts.idl.enums.Script import ScriptState
from lsst.ts.idl.enums.ScriptQueue import ScriptProcessState


class QueueState:
    def __init__(self):
        """State of the Script Queue for the User Interface model.
        """
        self.log = logging.getLogger(__name__)

        self.enabled = False
        self.running = False
        self._queue_script_indices = []
        self._past_script_indices = []
        self._current_script_index = 0

        self.scripts = {}

    def __call__(self, *args, **kwargs):
        return self.state

    @property
    def state(self):
        """Parse `self.running` property of the queue to a string.

        Returns
        -------
        state : `str`
            'Running' or 'Stopped'

        """
        return "Running" if self.running else "Stopped"

    @property
    def script_indices(self):
        """A list of indices for all scripts in the queue.

        Returns
        -------
        sal_indices : `list(int)`

        """
        sal_indices = []
        if self._current_script_index > 0:
            sal_indices = [self._current_script_index]

        for index in self._queue_script_indices:
            sal_indices.append(index)

        for index in self._past_script_indices:
            sal_indices.append(index)

        return sal_indices

    def update(self, queue):
        """Update using the current value of the `ScriptQueue` `queue` event.

        Parameters
        ----------
        queue : ``ScriptQueue.evt_queue.DataType``
            Queue state.
        """
        self.enabled = queue.enabled
        self.running = queue.running
        self._current_script_index = queue.currentSalIndex
        self._queue_script_indices = [queue.salIndices[i] for i in range(queue.length)]
        self._past_script_indices = [
            queue.pastSalIndices[i] for i in range(queue.pastLength)
        ]

        self.clear_scripts()

    def clear_scripts(self):
        """Remove items from `self.scripts` that are no longer in the queue.

        Script indices will be removed if not in `self._queue_script_indices`,
        `self._past_script_indices` or `self._current_script_index`.
        """
        current_indices = list(self.scripts.keys())
        for salindex in current_indices:
            if (
                salindex not in self._queue_script_indices
                and salindex not in self._past_script_indices
                and salindex != self._current_script_index
                and salindex < max(self._queue_script_indices, default=salindex)
            ):
                self.log.debug(f"Removing script {salindex}")
                del self.scripts[salindex]

    def update_script_info(self, script):
        """

        Parameters
        ----------
        script : ``ScriptQueue.evt_script.DataType``
            Script state.
        """
        s_type = "Standard" if script.isStandard else "External"

        if script.salIndex not in self.scripts:
            self.scripts[script.salIndex] = self.new_script(script.salIndex)

            self.scripts[script.salIndex]["type"] = s_type
            self.scripts[script.salIndex]["path"] = script.path
            self.scripts[script.salIndex][
                "timestamp_process_start"
            ] = script.timestampProcessStart
            self.scripts[script.salIndex][
                "timestamp_run_start"
            ] = script.timestampRunStart
            self.scripts[script.salIndex][
                "timestamp_configure_start"
            ] = script.timestampConfigureStart
            self.scripts[script.salIndex][
                "timestamp_configure_end"
            ] = script.timestampConfigureEnd
            self.scripts[script.salIndex][
                "timestamp_process_end"
            ] = script.timestampProcessEnd
            self.scripts[script.salIndex]["script_state"] = ScriptState(
                script.scriptState
            )
            self.scripts[script.salIndex]["process_state"] = ScriptProcessState(
                script.processState
            )
            self.scripts[script.salIndex]["updated"] = True

        else:
            self.scripts[script.salIndex]["type"] = s_type
            self.scripts[script.salIndex]["path"] = script.path
            self.scripts[script.salIndex][
                "timestamp_process_start"
            ] = script.timestampProcessStart
            self.scripts[script.salIndex][
                "timestamp_configure_start"
            ] = script.timestampConfigureStart
            self.scripts[script.salIndex][
                "timestamp_configure_end"
            ] = script.timestampConfigureEnd
            self.scripts[script.salIndex][
                "timestamp_run_start"
            ] = script.timestampRunStart
            self.scripts[script.salIndex][
                "timestamp_process_end"
            ] = script.timestampProcessEnd
            self.scripts[script.salIndex]["script_state"] = ScriptState(
                script.scriptState
            )
            self.scripts[script.salIndex]["process_state"] = ScriptProcessState(
                script.processState
            )
            self.scripts[script.salIndex]["updated"] = True

            # delete remote if script is done
            if (
                self.scripts[script.salIndex]["process_state"]
                >= ScriptProcessState.DONE
                and self.scripts[script.salIndex]["remote"] is not None
            ):
                del self.scripts[script.salIndex]["remote"]
                self.scripts[script.salIndex]["remote"] = None

    def new_script(self, salindex):
        """Return an empty dictionary with the definition of a script.

        Returns
        -------
        script : `dict`
        """
        return {
            "index": salindex,
            "type": "UNKNOWN",
            "path": "UNKNOWN",
            "timestamp_process_start": 0.0,
            "timestamp_run_start": 0.0,
            "timestamp_configure_start": 0.0,
            "timestamp_configure_end": 0.0,
            "timestamp_process_end": 0.0,
            "script_state": 0,
            "process_state": 0,
            "remote": None,
            "updated": False,
        }

    def add_script(self, salindex):
        """Add new script to the list of scripts.

        Parameters
        ----------
        salindex : `int`
            SAL index of added script
        """
        self.scripts[salindex] = self.new_script(salindex)

    def parse(self):
        """Parse the current queue state into a dictionary.

        Returns
        -------
        state : `dict`

        """

        state = {
            "state": self.state,
            "queue_scripts": {},
            "past_scripts": {},
            "current": None,
        }

        for index in self._queue_script_indices:
            if index in self.scripts:
                state["queue_scripts"][index] = self.scripts[index]
            else:
                state["queue_scripts"][index] = self.new_script(index)

        for index in self._past_script_indices:
            state["past_scripts"][index] = self.scripts[index]

        if self._current_script_index > 0:
            state["current"] = self.scripts[self._current_script_index]

        return state
