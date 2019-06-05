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

__all__ = ["BaseScript"]

import abc
import argparse
import asyncio
import re
import sys
import time
import types

import yaml

from lsst.ts import salobj
from lsst.ts.idl.enums.Script import MetadataCoordSys, MetadataRotSys, MetadataDome, ScriptState


HEARTBEAT_INTERVAL = 5  # seconds


def _make_remote_name(remote):
    """Make a remote name from a remote, for output as script metadata.

    Parameters
    ----------
    remote : `salobj.Remote`
        Remote
    """
    name = remote.salinfo.name
    index = remote.salinfo.index
    if index is not None:
        name = name + ":" + str(index)
    return name


class BaseScript(salobj.Controller, abc.ABC):
    """Abstract base class for :ref:`lsst.ts.scriptqueue_sal_scripts`.

    Parameters
    ----------
    index : `int`
        Index of SAL Script component. This must be unique among all
        SAL scripts that are currently running.
    descr : `str`
        Short description of what the script does, for operator display.

    Attributes
    ----------
    log : `logging.Logger`
        A Python log. You can safely log to it from different threads.
        Note that it can take up to ``LOG_MESSAGES_INTERVAL`` seconds
        before a log message is sent.
    """
    def __init__(self, index, descr):
        super().__init__("Script", index, do_callbacks=True)
        schema = self.schema
        if schema is None:
            self.config_validator = None
        else:
            self.config_validator = salobj.DefaultingValidator(schema=schema)
        self._run_task = None
        self._pause_future = None
        self.done_task = asyncio.Future()
        """A task that is set to None, or an exception if cleanup fails,
        when the task is done.
        """
        self._is_exiting = False
        self.evt_description.set(
            classname=type(self).__name__,
            description=str(descr),
        )
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        self.final_state_delay = 0.3
        """Delay (sec) to allow sending the final state and acknowledging
        the command before exiting."""

        self.timestamps = dict()
        """Dict of ScriptState: timestamp"""

    async def start(self):
        self_name = f"Script:{self.salinfo.index}"
        remote_names = set()
        start_tasks = []
        for salinfo in self.domain.salinfo_set:
            name = f"{salinfo.name}:{salinfo.index}"
            if name == self_name:
                continue
            remote_names.add(name)
            start_tasks.append(salinfo.start_task)

        await asyncio.gather(*start_tasks)
        await super().start()

        self.evt_state.set_put(state=ScriptState.UNCONFIGURED)
        self.evt_description.set_put(
            remotes=",".join(sorted(remote_names)),
            force_output=True,
        )

    @classmethod
    def main(cls, descr=None):
        """Start the script from the command line.

        Parameters
        ----------
        descr : `str` (optional)
            Short description of what the script does, for operator display.
            Leave at None if the script already has a description, which is
            the most common case. Primarily intended for unit tests,
            e.g. running ``TestScript``.


        The final return code will be:

        * 0 if final state is `ScriptState.DONE` or `ScriptState.STOPPED`
        * 1 if final state is `ScriptState.FAILED`
        * 2 otherwise (which should never happen)
        """
        parser = argparse.ArgumentParser(f"Run {cls.__name__} from the command line")
        parser.add_argument("index", type=int,
                            help="Script SAL Component index; must be unique among running Scripts")
        args = parser.parse_args()
        kwargs = dict(index=args.index)
        if descr is not None:
            kwargs["descr"] = descr
        script = cls(**kwargs)
        asyncio.get_event_loop().run_until_complete(script.done_task)
        return_code = {ScriptState.DONE: 0,
                       ScriptState.STOPPED: 0,
                       ScriptState.FAILED: 1}.get(script.state.state, 2)
        sys.exit(return_code)

    @property
    def checkpoints(self):
        """Get the checkpoints at which to pause and stop.

        Returns ``self.evt_checkpoints.data`` which has these fields:

        * ``pause``: checkpoints at which to pause, a regular expression
        * ``stop``: checkpoints at which to stop, a regular expression
        """
        return self.evt_checkpoints.data

    @property
    def state(self):
        """Get the current state.

        Returns ``self.evt_state.data``, which has these fields:

        * ``state``: the current state; a `ScriptState`
        * ``last_checkpoint``: name of most recently seen checkpoint;
          a `str`
        * ``reason``: reason for this state, if any; a `str`
        """
        return self.evt_state.data

    @property
    def state_name(self):
        """Get the current `state`.state as a name.
        """
        try:
            return ScriptState(self.state.state).name
        except ValueError:
            return f"UNKNOWN({self.state.state})"

    def set_state(self, state=None, reason=None, keep_old_reason=False, last_checkpoint=None,
                  force_output=False):
        """Set the script state.

        Parameters
        ----------
        state : `ScriptState` or `int` (optional)
            New state, or None if no change
        reason : `str` (optional)
            Reason for state change. `None` for no new reason.
        keep_old_reason : `bool`
            If True, keep old reason; append the ``reason`` argument after ";"
            if it is is a non-empty string.
            If False replace with ``reason``, or "" if ``reason`` is `None`.
        last_checkpoint : `str` (optional)
            Name of most recently seen checkpoint. None for no change.
        force_output : `bool` (optional)
            If True the output even if not changed.
        """
        if state is not None:
            state = ScriptState(state)
            self.timestamps[state] = time.time()
        if keep_old_reason and reason is not None:
            sepstr = "; " if self.evt_state.data.reason else ""
            reason = self.evt_state.data.reason + sepstr + reason
        self.evt_state.set_put(
            state=state,
            reason=reason,
            lastCheckpoint=last_checkpoint,
            force_output=force_output)

    async def checkpoint(self, name=""):
        """Await this at any "nice" point your script can be paused or stopped.

        Parameters
        ----------
        name : `str` (optional)
            Name of checkpoint; "" if it has no name.

        Raises
        ------
        RuntimeError:
            If the state is not `ScriptState.RUNNING`. This likely means
            you called checkpoint from somewhere other than `run`.
        RuntimeError:
            If `_run_task` is `None` or done. This probably means your code
            incorrectly set the state.
        """
        if not self.state.state == ScriptState.RUNNING:
            raise RuntimeError(f"checkpoint error: state={self.state_name} instead of RUNNING; "
                               "did you call checkpoint from somewhere other than `run`?")
        if self._run_task is None:
            raise RuntimeError(f"checkpoint error: state is RUNNING but no run_task")
        if self._run_task.done():
            raise RuntimeError(f"checkpoint error: state is RUNNING but run_task is done")

        if re.fullmatch(self.checkpoints.stop, name):
            self.set_state(ScriptState.STOPPING, last_checkpoint=name)
            raise asyncio.CancelledError(
                f"stop by request: checkpoint {name} matches {self.checkpoints.stop}")
        elif re.fullmatch(self.checkpoints.pause, name):
            self._pause_future = asyncio.Future()
            self.set_state(ScriptState.PAUSED, last_checkpoint=name)
            await self._pause_future
            self.set_state(ScriptState.RUNNING)
        else:
            self.set_state(last_checkpoint=name, force_output=True)
            await asyncio.sleep(0.001)

    async def close_tasks(self):
        self._is_exiting = True
        await super().close_tasks()
        self._heartbeat_task.cancel()
        if self._run_task is not None:
            self._run_task.cancel()
        if self._pause_future is not None:
            self._pause_future.cancel()
        # Do not cancel done_task because that messes up normal script exit,
        # which has a significant delay before setting that task done.

    @abc.abstractmethod
    async def configure(self, config):
        """Configure the script.

        Parameters
        ----------
        config : `types.SimpleNamespace`
            Configuration.

        Notes
        -----
        This method is called by `do_configure``.
        The script state will be `ScriptState.UNCONFIGURED`.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def set_metadata(self, metadata):
        """Set metadata fields in the provided struct, given the
        current configuration.

        Parameters
        ----------
        metadata : ``self.evt_metadata.DataType()``
            Metadata to update. Set those fields for which
            you have useful information.

        Notes
        -----
        This method is called after `configure` by `do_configure`.
        The script state will be `ScriptState.UNCONFIGURED`.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    async def run(self):
        """Run the script.

        Your subclass must provide an implementation, as follows:

        * At points where you support pausing call `checkpoint`.
        * Raise an exception on error. Raise `salobj.ExpectedError`
          to avoid logging a traceback.

        Notes
        -----
        This method is only called when the script state is
        `ScriptState.CONFIGURED`. The remaining state transitions
        are handled automatically.
        """
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def schema(self):
        """Return a jsonschema to validate configuration, as a `dict`.

        Please provide default values for all fields for which defaults
        make sense. This makes the script easier to use.

        If your script has no configuration then return `None`,
        in which case the ``config`` field of the ``configure`` command
        must be an empty string.
        """
        raise NotImplementedError()

    async def cleanup(self):
        """Perform final cleanup, if any.

        This method is always called as the script state is exiting
        (unless the script process is aborted by SIGTERM or SIGKILL).
        """
        pass

    def assert_state(self, action, states):
        """Assert that the current state is in ``states`` and the script
        is not exiting.

        Parameters
        ----------
        action : `string`
            Description of what you want to do.
        states : `list` of `salobj.ScriptState`
            The required state.
        """
        if self._is_exiting:
            raise salobj.ExpectedError(f"Cannot {action}: script is exiting")
        if self.state.state not in states:
            states_str = ", ".join(s.name for s in states)
            raise salobj.ExpectedError(
                f"Cannot {action}: state={self.state_name} instead of {states_str}")

    async def do_configure(self, data):
        """Configure the currently loaded script.

        Parameters
        ----------
        data : ``cmd_configure.DataType``
            Configuration.

        Raises
        ------
        salobj.ExpectedError
            If `state`.state is not `ScriptState.UNCONFIGURED`.

        Notes
        -----
        This method does the following:

        * Parse the ``config`` field as yaml-encoded `dict` and validate it
          (including setting default values).
        * Call `configure`.
        * Call `set_metadata`.
        * Output the metadata event.
        * Change the script state to `ScriptState.CONFIGURED`.
        """
        self.assert_state("configure", [ScriptState.UNCONFIGURED])
        try:
            if self.config_validator is None:
                if data.config:
                    raise RuntimeError("This script has no configuration; "
                                       f"config={data.config} must be empty.")
                config = types.SimpleNamespace()
            else:
                if data.config:
                    user_config_dict = yaml.safe_load(data.config)
                else:
                    user_config_dict = {}
                full_config_dict = self.config_validator.validate(user_config_dict)
                config = types.SimpleNamespace(**full_config_dict)
            await self.configure(config)
        except Exception as e:
            errmsg = f"config({data.config}) failed"
            self.log.exception(errmsg)
            raise salobj.ExpectedError(f"{errmsg}: {e}") from e

        metadata = self.evt_metadata.DataType()
        # initialize to vaguely reasonable values
        metadata.coordinateSystem = MetadataCoordSys.NONE
        metadata.rotationSystem = MetadataRotSys.NONE
        metadata.filters = ""  # any
        metadata.dome = MetadataDome.EITHER
        metadata.duration = 0
        self.set_metadata(metadata)
        self.evt_metadata.put(metadata)
        self.set_state(ScriptState.CONFIGURED)
        await asyncio.sleep(0.001)

    async def do_run(self, data):
        """Run the configured script and quit.

        Parameters
        ----------
        data : ``cmd_run.DataType``
            Ignored.

        Raises
        ------
        salobj.ExpectedError
            If `state`.state is not `ScriptState.CONFIGURED`.
        """
        self.assert_state("run", [ScriptState.CONFIGURED])
        try:
            self.set_state(ScriptState.RUNNING)
            self._run_task = asyncio.ensure_future(self.run())
            await self._run_task
            self.set_state(ScriptState.ENDING)
        except asyncio.CancelledError:
            if self.state.state != ScriptState.STOPPING:
                self.set_state(ScriptState.STOPPING)
        except Exception as e:
            if not isinstance(e, salobj.ExpectedError):
                self.log.exception("Error in run")
            self.set_state(ScriptState.FAILING, reason=f"Error in run: {e}")
        await asyncio.sleep(0.001)
        await self._exit()

    def do_resume(self, data):
        """Resume the currently paused script.

        Parameters
        ----------
        data : ``cmd_resume.DataType``
            Ignored.

        Raises
        ------
        salobj.ExpectedError
            If `state`.state is not `ScriptState.PAUSED`.
        """
        self.assert_state("resume", [ScriptState.PAUSED])
        self._pause_future.set_result(None)

    def do_setCheckpoints(self, data):
        """Set or clear the checkpoints at which to pause and stop.

        Parameters
        ----------
        data : ``cmd_setCheckpoints.DataType``
            Names of checkpoints for pausing and stopping, each a single
            regular expression; "" for no checkpoints, ".*" for all.

        Raises
        ------
        salobj.ExpectedError
            If `state`.state is not `ScriptState.UNCONFIGURED`,
            `ScriptState.CONFIGURED`, `ScriptState.RUNNING`
            or `ScriptState.PAUSED`.
        """
        self.assert_state("setCheckpoints", [ScriptState.UNCONFIGURED, ScriptState.CONFIGURED,
                          ScriptState.RUNNING, ScriptState.PAUSED])
        try:
            re.compile(data.stop)
        except Exception as e:
            raise salobj.ExpectedError(f"stop={data.stop!r} not a valid regex: {e}")
        try:
            re.compile(data.pause)
        except Exception as e:
            raise salobj.ExpectedError(f"pause={data.pause!r} not a valid regex: {e}")
        self.evt_checkpoints.set_put(
            pause=data.pause,
            stop=data.stop,
            force_output=True,
        )

    async def do_stop(self, data):
        """Stop the script.

        Parameters
        ----------
        data : ``cmd_stop.DataType``
            Ignored.

        Notes
        -----
        This is a no-op if the script is already exiting.
        This does not wait for _exit to run.
        """
        if self._is_exiting:
            return
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        else:
            self.set_state(state=ScriptState.STOPPING)
            await self._exit()

    async def _heartbeat_loop(self):
        """Output heartbeat at regular intervals.
        """
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                self.evt_heartbeat.put()
            except asyncio.CancelledError:
                break
            except Exception:
                self.log.exception("Heartbeat output failed")

    async def _exit(self):
        """Call cleanup (if the script was run) and exit the script.
        """
        if self._is_exiting:
            return
        self._is_exiting = True
        try:
            if self._run_task is not None:
                await self.cleanup()
            self._heartbeat_task.cancel()

            reason = None
            final_state = {
                ScriptState.ENDING: ScriptState.DONE,
                ScriptState.STOPPING: ScriptState.STOPPED,
                ScriptState.FAILING: ScriptState.FAILED,
            }.get(self.state.state)
            if final_state is None:
                reason = f"unexpected state for _exit {self.state_name}"
                final_state = ScriptState.FAILED

            self.log.info(f"Setting final state to {final_state!r}")
            self.set_state(final_state, reason=reason, keep_old_reason=True)
            await asyncio.sleep(self.final_state_delay)
            asyncio.ensure_future(self.close())
        except Exception as e:
            if not isinstance(e, salobj.ExpectedError):
                self.log.exception("Error in run")
            self.set_state(ScriptState.FAILED, reason=f"failed in _exit: {e}", keep_old_reason=True)
            await asyncio.sleep(self.final_state_delay)
            asyncio.ensure_future(self.close(e))
