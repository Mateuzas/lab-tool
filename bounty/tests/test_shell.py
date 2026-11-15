"""host/bounty shell (CONTRACTS §12.1) and bounty-run's shell handling. Docker is always mocked;
handles and hosts are fake (.test), nothing contacts a real program."""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from bounty import cli, guard, shell
from bounty.util import REPO_ROOT, BountyError

SHARE = os.path.join(REPO_ROOT, "container", "share", "bounty")


def load_bounty_run():
    path = os.path.join(REPO_ROOT, "container", "bin", "bounty-run")
    with mock.patch.dict(os.environ, {"BOUNTY_SHARE": SHARE}):
        loader = importlib.machinery.SourceFileLoader("bounty_run_under_test", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


class Shell(unittest.TestCase):
    def run_shell(self, handle=None, running=True, state=None, docker_rc=0):
        calls, execs = [], []

        def fake_docker(args, check=False, **kw):
            calls.append(args)
            if check and docker_rc != 0:
                raise BountyError("docker", "install failed", 1)
            return docker_rc, "", ""

        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(guard, "running", return_value=running), \
                mock.patch.object(guard, "read_state", return_value=state), \
                mock.patch.object(guard, "_docker", side_effect=fake_docker):
            shell.shell(handle, execvp=lambda f, a: execs.append((f, a)))
        return calls, execs

    def code(self, **kw):
        try:
            self.run_shell(**kw)
        except BountyError as e:
            return e.exit_code
        return 0

    def test_not_running_exits_3_without_docker_exec(self):
        with mock.patch.object(guard, "_docker", side_effect=AssertionError("no exec")):
            self.assertEqual(self.code(running=False), 3)

    def test_no_active_program_exits_5(self):
        self.assertEqual(self.code(state={"status": "no-profile", "program": None}), 5)
        self.assertEqual(self.code(state=None), 5)
        # --handle never stands in for an active program
        self.assertEqual(self.code(handle="intigriti-acme", state={"status": "no-profile"}), 5)

    def test_active_handle_mkdir_then_exec(self):
        calls, execs = self.run_shell(state={"status": "active", "program": "intigriti-acme"})
        self.assertEqual(calls, [["exec", "-u", "root", "kali-bounty", "install", "-d", "-o", "hunter", "-g",
                                  "hunter", "/bounty/work/intigriti-acme"]])
        self.assertEqual(execs, [("docker", ["docker", "exec", "-it", "-e", "BOUNTY_HANDLE=intigriti-acme",
                                             "-w", "/bounty/work/intigriti-acme", "kali-bounty", "bounty-run",
                                             "--actor", "human", "--", "bash", "-l"])])

    def test_handle_overrides_work_dir_only(self):
        calls, execs = self.run_shell(handle="Manual-Friend", state={"status": "active", "program": "intigriti-acme"})
        self.assertEqual(calls[0][-1], "/bounty/work/manual-friend")
        args = execs[0][1]
        self.assertIn("BOUNTY_HANDLE=manual-friend", args)
        self.assertEqual(args[args.index("-w") + 1], "/bounty/work/manual-friend")
        self.assertEqual(args[args.index("--actor") + 1], "human")   # same actor, same guard

    def test_bad_handle_is_usage_and_never_execs(self):
        for bad in ("../etc", "a b", "-rf", "x;id"):
            with self.subTest(bad=bad):
                self.assertEqual(self.code(handle=bad, state={"program": "intigriti-acme"}), 64)

    def test_mkdir_failure_does_not_exec(self):
        execs = []
        with mock.patch.object(guard, "running", return_value=True), \
                mock.patch.object(guard, "read_state", return_value={"program": "intigriti-acme"}), \
                mock.patch.object(guard, "_docker", side_effect=BountyError("docker", "install failed", 1)):
            with self.assertRaises(BountyError):
                shell.shell(None, execvp=lambda f, a: execs.append(a))
        self.assertEqual(execs, [])

    def test_cli_exit_codes(self):
        with mock.patch.object(guard, "running", return_value=False), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["shell"]), 3)
        self.assertIn("start the guard first", err.getvalue())
        with mock.patch.object(guard, "running", return_value=True), \
                mock.patch.object(guard, "read_state", return_value={"status": "no-profile"}), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["shell", "--handle", "intigriti-acme"]), 5)
        self.assertIn("pick a program with u / program use", err.getvalue())


class BountyRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = load_bounty_run()

    def test_shell_handle_validated(self):
        for value, want in (("intigriti-acme", "intigriti-acme"), ("../x", None), ("", None), ("A", None)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"BOUNTY_HANDLE": value}):
                self.assertEqual(self.br.shell_handle(), want)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.br.shell_handle())

    def test_login_bash_gets_rcfile_for_human_shell_only(self):
        rc = os.path.join(SHARE, "bashrc")
        with mock.patch.object(self.br, "SHELLRC", rc):
            self.assertEqual(self.br.shell_command("human", ["bash", "-l"], "intigriti-acme"),
                             ["bash", "--rcfile", rc, "-i"])
            self.assertEqual(self.br.shell_command("human", ["bash", "-l"], None), ["bash", "-l"])
            self.assertEqual(self.br.shell_command("claude", ["bash", "-l"], "intigriti-acme"), ["bash", "-l"])
            self.assertEqual(self.br.shell_command("human", ["bash", "-c", "id"], "intigriti-acme"),
                             ["bash", "-c", "id"])
            self.assertEqual(self.br.shell_command("human", ["curl", "https://app.acme.test/"], "intigriti-acme"),
                             ["curl", "https://app.acme.test/"])
        with mock.patch.object(self.br, "SHELLRC", "/nonexistent/bashrc"):
            self.assertEqual(self.br.shell_command("human", ["bash", "-l"], "intigriti-acme"), ["bash", "-l"])

    def test_work_dir_prefers_shell_handle(self):
        base = tempfile.mkdtemp()
        try:
            real_join, real_isdir = os.path.join, os.path.isdir

            def join(a, *rest):
                return real_join(base if a == "/bounty/work" else a, *rest)

            def isdir(p):
                return real_isdir(base if p == "/bounty/work" else p)
            with mock.patch.object(self.br.os.path, "join", side_effect=join), \
                    mock.patch.object(self.br.os.path, "isdir", side_effect=isdir), \
                    mock.patch.object(self.br.os, "chown"), \
                    mock.patch.object(self.br, "SNAPSHOT", "/nonexistent/policy.json"):
                self.assertEqual(self.br.work_dir("manual-friend"), real_join(base, "manual-friend"))
                self.assertTrue(real_isdir(real_join(base, "manual-friend", "recon")))
                self.assertEqual(self.br.work_dir(None), "/bounty/work")   # no snapshot handle
        finally:
            shutil.rmtree(base)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_bashrc_sets_prompt(self):
        home = tempfile.mkdtemp()
        try:
            with open(os.path.join(home, ".profile"), "w") as f:
                f.write("PS1='kali> '\n")   # like Kali's .bashrc: overwrites any PS1 from the environment
            out = subprocess.run(["bash", "--norc", "--noprofile", "-c",
                                  '. "$1"; printf %s "$PS1"', "bash", os.path.join(SHARE, "bashrc")],
                                 env={"HOME": home, "PATH": "/usr/bin:/bin", "BOUNTY_HANDLE": "intigriti-acme",
                                      "PS1": "x"},
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout.decode()
            self.assertEqual(out, "[intigriti-acme] \\w $ ")
        finally:
            shutil.rmtree(home)


if __name__ == "__main__":
    unittest.main()
