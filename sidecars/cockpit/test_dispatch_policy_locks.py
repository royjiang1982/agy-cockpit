"""Atomic dispatch policy writes, revision conflicts, and on-disk write protection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import json
import multiprocessing
import stat
from threading import Barrier
from unittest.mock import patch

import dispatch_policy as dp
from test_dispatch_policy import DispatchFixture

TIMEOUT = 10


def _process_write(path, reset, sender):
    real_flock = fcntl.flock

    def observe_lock(fd, operation):
        if operation != fcntl.LOCK_EX:
            return real_flock(fd, operation)
        # Ask the kernel whether this exact descriptor can be locked while the
        # parent holds its lock. Report contention, then take the real blocking path.
        try:
            real_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            sender.send("blocked")
            real_flock(fd, operation)
        sender.send("acquired")

    with sender, patch.object(dp.fcntl, "flock", observe_lock):
        sender.send("ready")
        try:
            if reset:
                dp.reset_policy(expected_revision=1, policy_path=path)
            else:
                dp.save_policy(dict(dp.default_policy(), enabled=False), expected_revision=1, policy_path=path)
        except dp.PolicyConflictError:
            sender.send("PolicyConflictError")
        else:
            sender.send("saved")


class DispatchPolicyLockTests(DispatchFixture):
    def test_degraded_files_are_immutable_for_save_and_reset(self):
        missing = dp.load_policy(self.policy_path)
        self.assertFalse(missing["exists"])
        self.assertFalse(missing["degraded"])
        for contents in (
            "{", "\n".join(["{", '  "revision": 1, "revision": 2', "}"]),
            json.dumps(dict(dp.default_policy(), schema_version=9)),
        ):
            with self.subTest(contents=contents[:15]):
                self.policy_path.write_text(contents)
                current = dp.load_policy(self.policy_path)
                self.assertTrue(current["exists"])
                self.assertTrue(current["degraded"])
                self.assertTrue(current["read_only_warning"])
                self.assertEqual(current["policy"], dp.default_policy())
                with self.assertRaises(dp.PolicyConflictError):
                    dp.save_policy(current["policy"], expected_revision=1, policy_path=self.policy_path)
                with self.assertRaises(dp.PolicyConflictError):
                    dp.reset_policy(expected_revision=1, policy_path=self.policy_path)
                self.assertEqual(self.policy_path.read_text(), contents)

    def test_concurrent_save_is_atomic_and_reset_advances_revision(self):
        gate = Barrier(2)

        def update(enabled):
            p = dict(dp.default_policy(), enabled=enabled)
            gate.wait(timeout=TIMEOUT)
            try:
                return dp.save_policy(p, expected_revision=1, policy_path=self.policy_path)
            except dp.PolicyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [True, False]))
        winner, = [result for result in results if result is not None]
        self.assertEqual(winner["revision"], 2)
        self.assertEqual(dp.load_policy(self.policy_path)["policy"], winner)
        self.assertEqual(stat.S_IMODE(self.policy_path.stat().st_mode), 0o600)
        self.assertEqual({p.name for p in self.policy_path.parent.iterdir()},
                         {self.policy_path.name, self.policy_path.name + ".lock"})
        with self.assertRaises(dp.PolicyConflictError):
            dp.reset_policy(expected_revision=1, policy_path=self.policy_path)
        self.assertEqual(dp.reset_policy(expected_revision=2, policy_path=self.policy_path),
                         dict(dp.default_policy(), revision=3))

    def test_concurrent_processes_share_file_lock_and_recheck_revision(self):
        ctx = multiprocessing.get_context("spawn")
        lock_path = self.policy_path.with_name(self.policy_path.name + ".lock")
        for second_resets, replace_schema in ((False, False), (True, False), (True, True)):
            with self.subTest(second_resets=second_resets, replace_schema=replace_schema), ExitStack() as stack:
                self.policy_path.unlink(missing_ok=True)
                procs, receivers = [], []
                with lock_path.open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    for reset in (False, second_resets):
                        recv, send = ctx.Pipe(duplex=False)
                        stack.enter_context(recv)
                        proc = ctx.Process(target=_process_write, args=(self.policy_path, reset, send))
                        with send:
                            proc.start()
                        stack.callback(self.stop_process, proc)
                        procs.append(proc)
                        receivers.append(recv)
                    for recv in receivers:
                        self.assertEqual(self.receive(recv), "ready")
                        self.assertEqual(self.receive(recv), "blocked",
                                         "writer bypassed another process's file lock")
                    self.assertFalse(self.policy_path.exists())
                    if replace_schema:
                        external_contents = json.dumps(dict(dp.default_policy(), schema_version=9))
                        incoming = self.policy_path.with_name("external.json")
                        incoming.write_text(external_contents)
                        incoming.replace(self.policy_path)
                results = []
                for proc, recv in zip(procs, receivers):
                    self.assertEqual(self.receive(recv), "acquired")
                    results.append(self.receive(recv))
                    proc.join(TIMEOUT)
                    self.assertEqual(proc.exitcode, 0)
                if replace_schema:
                    self.assertEqual(results, ["PolicyConflictError"] * 2)
                    self.assertEqual(self.policy_path.read_text(), external_contents)
                else:
                    self.assertCountEqual(results, ["saved", "PolicyConflictError"])
                    self.assertEqual(dp.load_policy(self.policy_path)["policy"]["revision"], 2)

    def receive(self, receiver):
        self.assertTrue(receiver.poll(TIMEOUT), "writer did not signal")
        return receiver.recv()

    @staticmethod
    def stop_process(proc):
        if proc.is_alive():
            proc.terminate()
        proc.join(TIMEOUT)
        if proc.is_alive():
            proc.kill()
            proc.join(TIMEOUT)
        proc.close()
