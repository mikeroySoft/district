"""Systemd path boundaries with temporary definitions and fake managers only."""
import json
import os
import shlex
import subprocess
import sys
import unittest

from district import host, service, systemd, update_transaction
from tests import test_one_install as fixtures


class SystemdPathsTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.OneInstallTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_installed_service_executes_complete_paths_and_environment(self):
        case = self.case
        root = case.root / 'Work "quotes" O\'Brien\\ folder %data $ROOT'
        root.mkdir()
        executable = root / 'launcher "O\'Brien"\\ 50%$name'
        executable.write_text(f'#!{sys.executable}\nimport json,os,sys\nprint(json.dumps([sys.argv,os.getcwd(),os.environ["TOOL_HOME"]]))\n')
        executable.chmod(0o700)
        argument = str(root / 'input "report".txt')
        expected_env = str(root / 'Engine Alias')
        observed = []
        definition = service.ServiceDefinition(
            label='district-path-check', description='Temporary path check',
            argv=(str(executable), argument), root=root,
            environment={'TOOL_HOME': expected_env})
        unit_dir = case.root / 'Unit Directory'

        def manager(argv, **kwargs):
            if argv[2] == 'start':
                text = (unit_dir / 'district-path-check.service').read_text()
                values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
                decoded = lambda value: [word.replace('%%', '%') for word in systemd.words(value)]
                command = [word.replace('$$', '$') for word in decoded(values['ExecStart'])]
                environment = dict(os.environ)
                environment.update(item.split('=', 1) for item in decoded(values['Environment']))
                working, = decoded(values['WorkingDirectory'])
                result = subprocess.run(command, cwd=working, env=environment,
                                        capture_output=True, text=True, timeout=5)
                observed.append(json.loads(result.stdout))
                return result
            self.assertEqual(argv[2], 'daemon-reload')
            return subprocess.CompletedProcess(argv, 0, '', '')

        backend = service.SystemdUserBackend(manager, unit_dir=unit_dir)
        backend.install(definition)
        backend.start('district-path-check.service', timeout=5)
        self.assertEqual(observed, [[[str(executable), argument], str(root), expected_env]])

    def test_status_preserves_quoted_and_escaped_file_values_locally_and_remotely(self):
        case = self.case
        executable = str(case.root / 'Engine Alias/python')
        escaped = executable.replace('Engine Alias', r'Engine\x20Alias')
        env_path = str(case.root / 'Tool Home')
        body = f'[Service]\nExecStart="{escaped}" -m factory dispatch\nEnvironment="TOOL_HOME={env_path}" "SECOND=two words"\n'
        unit_dir = case.root / 'Unit Directory'
        unit_dir.mkdir()
        (unit_dir / 'factory-paths.service').write_text(body)

        def manager(argv, **kwargs):
            if argv[0] == 'python3':
                return subprocess.run([sys.executable, *argv[1:]], capture_output=True,
                                      text=True, timeout=5, input=kwargs.get('input'))
            self.assertEqual(argv[0], 'systemctl')
            prop = next((arg.partition('=')[2] for arg in argv if arg.startswith('--property=')), '')
            output = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                      'UnitFileState': 'enabled', 'MainPID': '0'}.get(prop, '')
            return subprocess.CompletedProcess(argv, 0, output, '')

        for remote in (False, True):
            with self.subTest(remote=remote):
                if remote:
                    def transport(argv, **kwargs):
                        parts = shlex.split(argv[-1])
                        if parts[:2] == ['python3', '-c'] and parts[2] == host.REMOTE_DEADLINE:
                            parts = shlex.split(parts[-1])
                        return manager(parts, **kwargs)
                    runner = host.RemoteRunner('fake', 'unused', transport=transport)
                    directory = runner.path(str(unit_dir))
                else:
                    runner, directory = manager, unit_dir
                backend = service.SystemdUserBackend(runner, unit_dir=directory)
                status = backend.status('factory-paths.service', timeout=5)
                self.assertEqual(status.argv, (executable, '-m', 'factory', 'dispatch'))
                self.assertEqual(status.environment, {'TOOL_HOME': env_path, 'SECOND': 'two words'})

    def test_apply_accepts_systemd_escaped_canonical_tool_path(self):
        case = self.case
        case.tool = case.root / 'Application Support/FactoryTools/factory'
        os.environ['UV_TOOL_DIR'] = str(case.tool.parent)
        case.install_receipt(fixtures.TARGET)
        escaped = str(case.tool / 'bin/python').replace('Application Support', r'Application\x20Support')
        case.rendered = lambda: ('factory-widgets.service', f'[Service]\nExecStart="{escaped}" -m factory dispatch\n')
        case.backend.definitions_changed.return_value = True
        self.assertEqual(case.apply(), 0, case.output.getvalue())
        self.assertTrue((host.unit_dir() / 'factory-widgets.service').exists())
        self.assertEqual(case.installs(), [])
        self.assertEqual(case.active, {'factory-widgets.timer'})

    def test_ambiguous_live_systemd_debug_argv_is_not_trusted(self):
        # systemctl's human-readable argv[] format has no argument boundaries.
        for folder, suffix in (('Engine Alias', ''), ('engine', " --host '127.0.0.1'")):
            with self.subTest(folder=folder, suffix=suffix):
                tool = self.case.root / folder
                executable = str(tool / 'bin/python3')
                raw = f'{{ path={executable} ; argv[]={executable} -m district dashboard{suffix} ; ignore_errors=no ; start_time=[n/a] ; }}'
                def manager(argv, **kwargs):
                    prop = next((arg.partition('=')[2] for arg in argv if arg.startswith('--property=')), '')
                    output = {'LoadState': 'loaded', 'ActiveState': 'active',
                              'MainPID': '42', 'ExecStart': raw}.get(prop, '')
                    return subprocess.CompletedProcess(argv, 0, output, '')
                backend = service.SystemdUserBackend(manager, unit_dir=self.case.root / 'units')
                with self.assertRaisesRegex(update_transaction.TransactionError, 'does not use'):
                    update_transaction.dashboard_argv({'tool': str(tool)}, backend)


if __name__ == '__main__':
    unittest.main()
