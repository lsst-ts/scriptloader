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

__all__ = ["ScriptQueueCommander"]

import logging

from lsst.ts.idl.enums.ScriptQueue import Location
from lsst.ts.idl.enums.Script import ScriptState
from lsst.ts import salobj

ADD_TIMEOUT = 5  # Timeout for the add command (seconds).


class ScriptQueueCommander(salobj.CscCommander):
    def __init__(self, **kwargs):
        super().__init__(name="ScriptQueue", **kwargs)
        self.help_dict[
            "add"
        ] = """type path config  # add a script to the end of the queue:
    • type = s or std for standard, e or ext for external
    • config = @yaml_file_path or keyword1=value1 keyword2=value2 ..."""
        self.help_dict["showSchema"] = "type path  # type=s, std, e, or ext"
        self.help_dict[
            "stopScripts"
        ] = "sal_index1 [sal_index2 [... sal_indexN]] terminate (0 or 1)"
        self.script_remote = salobj.Remote(
            domain=self.domain,
            name="Script",
            index=0,
            readonly=True,
            include=["logMessage", "state"],
        )
        self.script_remote.evt_logMessage.callback = self.script_log_message
        self.script_remote.evt_state.callback = self.script_state
        # Dict of "type" argument: isStandard
        self.script_type_dict = dict(
            s=True, std=True, standard=True, e=False, ext=False, external=False
        )

    async def start(self):
        await super().start()
        await self.script_remote.start_task

    def get_is_standard(self, script_type):
        """Convert a script type argument to isStandard bool.
        """
        try:
            return self.script_type_dict[script_type]
        except KeyError:
            raise KeyError(
                f"type {script_type!r} must be one of {list(self.script_type_dict.keys())}"
            )

    def evt_availableScripts_callback(self, data):
        standard_scripts = data.standard.split(":")
        external_scripts = data.external.split(":")
        print("standard scripts:")
        for name in standard_scripts:
            print(f"• {name}")
        print("external scripts:")
        for name in external_scripts:
            print(f"• {name}")

    def evt_queue_callback(self, data):
        salIndices = data.salIndices[0 : data.length]
        pastSalIndices = data.pastSalIndices[0 : data.pastLength]
        print(
            f"{data.private_sndStamp:0.3f} queue "
            f"enabled={data.enabled}, "
            f"running={data.running}, "
            f"currentSalIndex={data.currentSalIndex}, "
            f"salIndices={salIndices}, "
            f"pastSalIndices={pastSalIndices}"
        )

    def script_log_message(self, data):
        exception_str = (
            (
                f", traceback={data.traceback}, "
                f"filePath={data.filePath}, "
                f"functionName={data.functionName}, "
                f"lineNumber={data.lineNumber}, "
            )
            if data.traceback
            else ""
        )
        print(
            f"{data.private_sndStamp:0.3f} Script:{data.ScriptID} "
            f"logMessage level={logging.getLevelName(data.level)}, "
            f"message={data.message}{exception_str}"
        )

    def script_state(self, data):
        try:
            state = ScriptState(data.state)
        except ValueError:
            state = data.state
        reason = f", reason={data.reason}" if data.reason else ""
        print(
            f"{data.private_sndStamp:0.3f} Script:{data.ScriptID} "
            f"state={state.name}{reason}, lastCheckpoint={data.lastCheckpoint}"
        )

    async def do_add(self, args):
        """Overrride the standard add command to simplify the interface.
        """
        if len(args) < 2:
            raise ValueError("Need at least 2 arguments")
        is_standard = self.get_is_standard(args[0])
        path = args[1]
        if len(args) == 2:
            config_yaml = ""
        elif len(args) == 3 and args[2].startswith("@"):
            config_path = args[2][1:]
            with open(config_path, "r") as f:
                config_yaml = f.read()
        else:
            config_yaml_items = []
            for config_arg in args[2:]:
                name_value = config_arg.split("=", 1)
                if len(name_value) != 2:
                    raise ValueError(f"Could not parse {config_arg!r} as keyword=value")
                name, value = name_value
                config_yaml_items.append(f"{name}: {value}")
            config_yaml = "\n".join(config_yaml_items)

        await self.remote.cmd_add.set_start(
            isStandard=is_standard,
            path=path,
            config=config_yaml,
            location=Location.LAST,
            timeout=ADD_TIMEOUT,
        )

    async def do_showSchema(self, args):
        """Overrride the standard showSchema command for named script type.
        """
        if len(args) != 2:
            raise ValueError("Need 2 arguments: type path")
        is_standard = self.get_is_standard(args[0])
        path = args[1]
        await self.remote.cmd_showSchema.set_start(
            isStandard=is_standard, path=path,
        )

    async def do_stopScripts(self, args):
        """Handle the stopScript command, which takes a list of script indices.
        """
        if len(args) < 2:
            raise ValueError("Need at least 2 arguments; sal_index terminate")
        terminate_str = args[-1]
        if terminate_str not in ("0", "1"):
            raise ValueError(f"terminate={terminate_str} must be 0 or 1")
        sal_indices = [int(sal_index) for sal_index in args[:-1]]
        terminate = bool(int(terminate_str))

        stop_data = self.remote.cmd_stopScripts.DataType()
        stop_data.length = len(sal_indices)
        stop_data.salIndices[0 : stop_data.length] = sal_indices
        stop_data.terminate = terminate
        await self.remote.cmd_stopScripts.start(data=stop_data)
