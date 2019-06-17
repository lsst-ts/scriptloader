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

import asyncio
import logging
import os
import subprocess
import unittest
import warnings

import yaml

from lsst.ts import salobj
from lsst.ts.idl.enums.Script import ScriptState
from lsst.ts.scriptqueue import BaseScript
from lsst.ts.scriptqueue.test_utils import TestScript

STD_TIMEOUT = 2
START_TIMEOUT = 30
END_TIMEOUT = 10

index_gen = salobj.index_generator()


class NonConfigurableScript(BaseScript):
    def __init__(self, index):
        super().__init__(index=index, descr="Non-configurable script")
        self.config = None
        self.run_called = False
        self.set_metadata_called = False

    @classmethod
    def get_schema(cls):
        return None

    async def configure(self, config):
        self.config = config

    async def run(self):
        self.run_called = True

    def set_metadata(self, metadata):
        self.set_metadata_called = True


class BaseScriptTestCase(unittest.TestCase):
    def setUp(self):
        salobj.set_random_lsst_dds_domain()
        self.datadir = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))
        self.index = next(index_gen)

    async def configure_script(self, script, **kwargs):
        """Configure a script by calling do_configure

        Parameters
        ----------
        script : `ts.scriptqueue.TestScript`
            A test script
        kwargs : `dict`
            A dict with one or more of the following keys:

            * ``wait_time`` (a float): how long to wait, in seconds
            * ``fail_run`` (bool): fail before waiting?
            * ``fail_cleanup`` (bool): fail in cleanup?

        Raises
        ------
        salobj.ExpectedError
            If ``kwargs`` includes other keywords than those
            documented above (``script.do_configure`` will raise
            that error). This can be useful for unit testing,
            but to try non-dict values you'll have to encode
            the yaml and call ``script.do_configure`` yourself.

        Notes
        -----
        If no keyword arguments are provided then ``script.do_configure``
        will be called with no config data (an empty string).
        This can be useful for unit testing.
        """
        if kwargs:
            # strip to remove final trailing newline
            config = yaml.safe_dump(kwargs).strip()
        else:
            config = ""
        configure_data = script.cmd_configure.DataType()
        configure_data.config = config
        await script.do_configure(configure_data)
        self.assertEqual(script.config.wait_time, kwargs.get("wait_time", 0))
        self.assertEqual(script.config.fail_run, kwargs.get("fail_run", False))
        self.assertEqual(script.config.fail_cleanup, kwargs.get("fail_cleanup", False))
        self.assertEqual(script.state.state, ScriptState.CONFIGURED)

    def test_get_schema(self):
        schema = TestScript.get_schema()
        self.assertTrue(isinstance(schema, dict))
        for name in ("$schema", "$id", "title", "description", "type", "properties"):
            self.assertIn(name, schema)
        self.assertFalse(schema["additionalProperties"])

    def test_non_configurable_script_get_schema(self):
        schema = NonConfigurableScript.get_schema()
        self.assertIsNone(schema)

    def test_non_configurable_script_empty_config(self):
        async def doit():
            async with NonConfigurableScript(index=self.index) as script:
                data = script.cmd_configure.DataType()
                await script.do_configure(data)
                self.assertEqual(len(script.config.__dict__), 0)
                self.assertTrue(script.set_metadata_called)
                self.assertFalse(script.run_called)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_non_configurable_script_invalid_config(self):
        async def doit():
            async with NonConfigurableScript(index=self.index) as script:
                data = script.cmd_configure.DataType()
                data.config = "invalid: should be empty"
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_configure(data)
                self.assertIsNone(script.config)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_setCheckpoints(self):

        async def doit():
            async with TestScript(index=self.index) as script:

                # try valid values
                data = script.cmd_setCheckpoints.DataType()
                for pause, stop in (
                    ("something", ""),
                    ("", "something_else"),
                    (".*", "start|end"),
                ):
                    data.pause = pause
                    data.stop = stop
                    script.do_setCheckpoints(data)
                    self.assertEqual(script.checkpoints.pause, pause)
                    self.assertEqual(script.checkpoints.stop, stop)

                # try with at least one checkpoint not a valid regex;
                # do_setCheckpoints should raise and not change the checkpoints
                initial_pause = "initial_pause"
                initial_stop = "initial_stop"
                data.pause = initial_pause
                data.stop = initial_stop
                script.do_setCheckpoints(data)
                for bad_pause, bad_stop in (
                    ("(", ""),
                    ("", "("),
                    ("[", "["),
                ):
                    data.pause = bad_pause
                    data.stop = bad_stop
                    with self.assertRaises(salobj.ExpectedError):
                        script.do_setCheckpoints(data)
                    self.assertEqual(script.checkpoints.pause, initial_pause)
                    self.assertEqual(script.checkpoints.stop, initial_stop)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_set_state_and_attributes(self):

        async def doit():
            async with TestScript(index=self.index) as script:
                # check keep_old_reason argument of set_state
                reason = "initial reason"
                additional_reason = "check append"
                script.set_state(reason=reason)
                script.set_state(reason=additional_reason, keep_old_reason=True)
                self.assertEqual(script.state.reason, reason + "; " + additional_reason)

                bad_state = 1 + max(s.value for s in ScriptState)
                with self.assertRaises(ValueError):
                    script.set_state(bad_state)
                script.state.state = bad_state
                self.assertEqual(script.state_name, f"UNKNOWN({bad_state})")
                self.assertFalse(script._is_exiting)

                script.set_state(ScriptState.CONFIGURED)
                self.assertEqual(script.state_name, "CONFIGURED")

                # check assert_states
                all_states = set(ScriptState)
                for state in ScriptState:
                    script.set_state(state)
                    self.assertEqual(script.state_name, state.name)
                    with self.assertRaises(salobj.ExpectedError):
                        script.assert_state("should fail because state not in allowed states",
                                            all_states - set([state]))

                    script.assert_state("should pass", [state])
                    script._is_exiting = True
                    with self.assertRaises(salobj.ExpectedError):
                        script.assert_state("should fail because exiting", [state])
                    script._is_exiting = False

                    # check that checkpoint is prohibited
                    # unless state is RUNNING
                    if state == ScriptState.RUNNING:
                        continue
                    with self.assertRaises(RuntimeError):
                        await script.checkpoint("foo")

                self.assertFalse(script.done_task.done())

        asyncio.get_event_loop().run_until_complete(doit())

    def test_pause(self):

        async def doit():
            async with TestScript(index=self.index) as script:
                # cannot run in UNCONFIGURED state
                run_data = script.cmd_run.DataType()
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_run(run_data)

                # test configure with data for a non-existent argument
                configure_data = script.cmd_configure.DataType()
                configure_data.config = "no_such_arg: 1"
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_configure(configure_data)
                self.assertEqual(script.state.state, ScriptState.UNCONFIGURED)

                # test configure with invalid yaml
                configure_data = script.cmd_configure.DataType()
                configure_data.config = "a : : 2"
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_configure(configure_data)
                self.assertEqual(script.state.state, ScriptState.UNCONFIGURED)

                # test configure with yaml that makes a string, not a dict
                configure_data = script.cmd_configure.DataType()
                configure_data.config = "just_a_string"
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_configure(configure_data)
                self.assertEqual(script.state.state, ScriptState.UNCONFIGURED)

                # test configure with yaml that makes a list, not a dict
                configure_data = script.cmd_configure.DataType()
                configure_data.config = "['not', 'a', 'dict']"
                with self.assertRaises(salobj.ExpectedError):
                    await script.do_configure(configure_data)
                self.assertEqual(script.state.state, ScriptState.UNCONFIGURED)

                # now real configuration
                wait_time = 0.5
                await self.configure_script(script, wait_time=wait_time)

                # set a pause checkpoint
                setCheckpoints_data = script.cmd_setCheckpoints.DataType()
                checkpoint_named_start = "start"
                checkpoint_that_does_not_exist = "nonexistent checkpoint"
                setCheckpoints_data.pause = checkpoint_named_start
                setCheckpoints_data.stop = checkpoint_that_does_not_exist
                script.do_setCheckpoints(setCheckpoints_data)
                self.assertEqual(script.checkpoints.pause, checkpoint_named_start)
                self.assertEqual(script.checkpoints.stop, checkpoint_that_does_not_exist)

                run_data = script.cmd_run.DataType()
                run_task = asyncio.ensure_future(script.do_run(run_data))
                niter = 0
                while script.state.state != ScriptState.PAUSED:
                    niter += 1
                    await asyncio.sleep(0)
                self.assertEqual(script.state.lastCheckpoint, checkpoint_named_start)
                self.assertEqual(script.checkpoints.pause, checkpoint_named_start)
                self.assertEqual(script.checkpoints.stop, checkpoint_that_does_not_exist)
                resume_data = script.cmd_resume.DataType()
                script.do_resume(resume_data)
                await asyncio.wait_for(run_task, 2)
                await asyncio.wait_for(script.done_task, timeout=END_TIMEOUT)
                duration = script.timestamps[ScriptState.ENDING] - script.timestamps[ScriptState.RUNNING]
                desired_duration = wait_time
                print(f"test_pause duration={duration:0.2f}")
                self.assertLess(abs(duration - desired_duration), 0.2)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_stop_at_checkpoint(self):
        async def doit():
            async with TestScript(index=self.index) as script:
                wait_time = 0.1
                await self.configure_script(script, wait_time=wait_time)

                # set a stop checkpoint
                setCheckpoints_data = script.cmd_setCheckpoints.DataType()
                checkpoint_named_end = "end"
                setCheckpoints_data.stop = checkpoint_named_end
                script.do_setCheckpoints(setCheckpoints_data)
                self.assertEqual(script.checkpoints.pause, "")
                self.assertEqual(script.checkpoints.stop, checkpoint_named_end)

                run_data = script.cmd_run.DataType()
                await asyncio.wait_for(script.do_run(run_data), 2)
                await asyncio.wait_for(script.done_task, timeout=END_TIMEOUT)
                self.assertEqual(script.state.lastCheckpoint, checkpoint_named_end)
                self.assertEqual(script.state.state, ScriptState.STOPPED)
                duration = script.timestamps[ScriptState.STOPPING] - script.timestamps[ScriptState.RUNNING]
                # waited and then stopped at the "end" checkpoint
                desired_duration = wait_time
                print(f"test_stop_at_checkpoint duration={duration:0.2f}")
                self.assertLess(abs(duration - desired_duration), 0.2)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_stop_while_paused(self):

        async def doit():
            async with TestScript(index=self.index) as script:
                wait_time = 5
                await self.configure_script(script, wait_time=wait_time)

                # set a stop checkpoint
                setCheckpoints_data = script.cmd_setCheckpoints.DataType()
                checkpoint_named_start = "start"
                setCheckpoints_data.pause = checkpoint_named_start
                script.do_setCheckpoints(setCheckpoints_data)
                self.assertEqual(script.checkpoints.pause, checkpoint_named_start)
                self.assertEqual(script.checkpoints.stop, "")

                run_data = script.cmd_run.DataType()
                asyncio.ensure_future(script.do_run(run_data))
                while script.state.lastCheckpoint != "start":
                    await asyncio.sleep(0)
                self.assertEqual(script.state.state, ScriptState.PAUSED)
                stop_data = script.cmd_stop.DataType()
                await script.do_stop(stop_data)
                await asyncio.wait_for(script.done_task, timeout=END_TIMEOUT)
                self.assertEqual(script.state.lastCheckpoint, checkpoint_named_start)
                self.assertEqual(script.state.state, ScriptState.STOPPED)
                duration = script.timestamps[ScriptState.STOPPING] - script.timestamps[ScriptState.RUNNING]
                # the script ran quickly because we stopped the script
                # just as soon as it paused at the "start" checkpoint
                desired_duration = 0
                print(f"test_stop_while_paused duration={duration:0.2f}")
                self.assertGreater(duration, 0.0)
                self.assertLess(abs(duration - desired_duration), 0.2)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_stop_while_running(self):

        async def doit():
            async with TestScript(index=self.index) as script:
                wait_time = 5
                pause_time = 0.5
                await self.configure_script(script, wait_time=wait_time)

                checkpoint_named_start = "start"
                run_data = script.cmd_run.DataType()
                asyncio.ensure_future(script.do_run(run_data))
                while script.state.lastCheckpoint != checkpoint_named_start:
                    await asyncio.sleep(0)
                self.assertEqual(script.state.state, ScriptState.RUNNING)
                await asyncio.sleep(pause_time)
                stop_data = script.cmd_stop.DataType()
                await script.do_stop(stop_data)
                await asyncio.wait_for(script.done_task, timeout=END_TIMEOUT)
                self.assertEqual(script.state.lastCheckpoint, checkpoint_named_start)
                self.assertEqual(script.state.state, ScriptState.STOPPED)
                duration = script.timestamps[ScriptState.STOPPING] - script.timestamps[ScriptState.RUNNING]
                # we waited `pause_time` seconds after the "start" checkpoint
                desired_duration = pause_time
                print(f"test_stop_while_running duration={duration:0.2f}")
                self.assertLess(abs(duration - desired_duration), 0.2)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_fail(self):
        wait_time = 0.1
        for fail_run in (False, True):  # vs fail_cleanup

            async def doit():
                async with TestScript(index=self.index) as script:
                    if fail_run:
                        await self.configure_script(script, fail_run=True)
                    else:
                        await self.configure_script(script, fail_cleanup=True)

                    desired_checkpoint = "start" if fail_run else "end"
                    run_data = script.cmd_run.DataType()
                    await asyncio.wait_for(script.do_run(run_data), 2)
                    await asyncio.wait_for(script.done_task, timeout=END_TIMEOUT)
                    self.assertEqual(script.state.lastCheckpoint, desired_checkpoint)
                    self.assertEqual(script.state.state, ScriptState.FAILED)
                    desired_end_state = ScriptState.FAILING if fail_run else ScriptState.STOPPING
                    duration = script.timestamps[desired_end_state] - script.timestamps[ScriptState.RUNNING]
                    # if fail_run then failed before waiting,
                    # otherwise failed after
                    desired_duration = 0 if fail_run else wait_time
                    print(f"test_fail duration={duration:0.2f} with fail_run={fail_run}")
                    self.assertLess(abs(duration - desired_duration), 0.2)

        asyncio.get_event_loop().run_until_complete(doit())

    def test_remote(self):
        """Test a script with remotes.

        Check that the remote_names attribute of the description event
        is properly set, and that the remotes have started when the
        script has started.
        """
        class ScriptWithRemotes(TestScript):
            def __init__(self, index, remote_indices):
                super().__init__(index, descr="Script with remotes")
                remotes = []
                # use remotes that read history here, despite the startup
                # overhead, to check that script.start_task
                # waits for the start_task in each remote.
                for rind in remote_indices:
                    remotes.append(salobj.Remote(domain=self.domain, name="Test", index=rind))
                self.remotes = remotes

        async def doit():
            remote_indices = [5, 7]
            async with ScriptWithRemotes(self.index, remote_indices) as script:
                remote_name_list = [f"Test:{ind}" for ind in remote_indices]
                desired_remote_names = ",".join(sorted(remote_name_list))
                self.assertEqual(script.evt_description.data.remotes, desired_remote_names)
                for remote in script.remotes:
                    self.assertTrue(remote.start_task.done())

        asyncio.get_event_loop().run_until_complete(doit())

    def test_script_process(self):
        """Test running a script as a subprocess.
        """
        script_path = os.path.join(self.datadir, "standard", "script1")

        async def doit():
            for fail in (None, "fail_run", "fail_cleanup"):
                with self.subTest(fail=fail):
                    async with salobj.Domain() as domain:
                        index = next(index_gen)
                        remote = salobj.Remote(domain=domain, name="Script", index=index,
                                               evt_max_history=0, tel_max_history=0)
                        await asyncio.wait_for(remote.start_task, timeout=STD_TIMEOUT)

                        def logcallback(data):
                            print(f"message={data.message}")
                        remote.evt_logMessage.callback = logcallback

                        process = await asyncio.create_subprocess_exec(script_path, str(index))
                        try:
                            self.assertIsNone(process.returncode)

                            state = await remote.evt_state.next(flush=False, timeout=START_TIMEOUT)
                            self.assertEqual(state.state, ScriptState.UNCONFIGURED)

                            logLevel_data = remote.evt_logLevel.get()
                            self.assertEqual(logLevel_data.level, logging.INFO)

                            wait_time = 0.1
                            configure_data = remote.cmd_configure.DataType()
                            config = f"wait_time: {wait_time}"
                            if fail:
                                config = config + f"\n{fail}: True"
                            print(f"config={config}")
                            configure_data.config = config
                            await remote.cmd_configure.start(configure_data, timeout=STD_TIMEOUT)

                            metadata = remote.evt_metadata.get()
                            self.assertEqual(metadata.duration, wait_time)
                            await asyncio.sleep(0.2)
                            log_msg = remote.evt_logMessage.get()
                            self.assertEqual(log_msg.message, "Configure succeeded")

                            await remote.cmd_run.start(timeout=STD_TIMEOUT)

                            await asyncio.wait_for(process.wait(), timeout=END_TIMEOUT)
                            if fail:
                                self.assertEqual(process.returncode, 1)
                            else:
                                self.assertEqual(process.returncode, 0)
                        finally:
                            if process.returncode is None:
                                process.terminate()
                                warnings.warn("Killed a process that was not properly terminated")

        asyncio.get_event_loop().run_until_complete(doit())

    def test_script_schema_process(self):
        """Test running a script with --schema as a subprocess.
        """
        script_path = os.path.join(self.datadir, "standard", "script1")

        async def doit():
            index = 1  # index is ignored
            process = await asyncio.create_subprocess_exec(script_path, str(index), "--schema",
                                                           stdout=subprocess.PIPE,
                                                           stderr=subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
                schema = yaml.safe_load(stdout)
                self.assertEqual(schema, TestScript.get_schema())
                self.assertEqual(stderr, b"")
                await asyncio.wait_for(process.wait(), timeout=END_TIMEOUT)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.returncode is None:
                    process.terminate()
                    warnings.warn("Killed a process that was not properly terminated")

        asyncio.get_event_loop().run_until_complete(doit())


if __name__ == "__main__":
    unittest.main()
