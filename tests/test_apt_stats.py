import subprocess
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import apt_stats
import system_stats


def origin(name='Ubuntu', archive='noble-security', label='Ubuntu', trusted=True):
    return NS(origin=name, archive=archive, label=label, trusted=trusted)


def version(number, origins=()):
    return NS(version=str(number), origins=origins)


def package(installed, candidate, versions, held=False):
    return NS(installed=installed, candidate=candidate, versions=versions,
              _pkg=NS(selected_state=1 if held else 0))


class AptStatsTests(unittest.TestCase):
    def test_security_origins(self):
        for value in (origin(), origin('Debian', 'stable-security', 'Debian-Security'),
                      origin('Debian', 'oldstable', 'Debian-Security'),
                      origin('Debian', 'buster/updates'),
                      origin('UbuntuESM', 'noble-infra-security'),
                      origin('UbuntuESMApps', 'noble-apps-security')):
            with self.subTest(value=value):
                self.assertTrue(apt_stats.security_origin(value))
        for value in (origin('LP-PPA-something'), origin(archive='noble-updates'),
                      origin(trusted=False), origin('Other', label='Debian-Security')):
            with self.subTest(value=value):
                self.assertFalse(apt_stats.security_origin(value))

    def test_counts_superseded_security_held_pins_and_uninstalled(self):
        old = version(1)
        fix = version(2, [origin(), origin()])
        newer = version(3, [origin(archive='noble-updates')])
        packages = [
            package(old, newer, [newer, fix, old], held=True),
            package(old, newer, [newer, old]),
            package(old, old, [fix, old]),  # pinned at installed version
            package(old, version(0), [version(0), old]),  # downgrade
            package(None, fix, [fix]),
            package(old, None, [old]),
            package(fix, newer, [newer, fix]),  # fix already installed
        ]
        counts = apt_stats.count_updates(packages, lambda a, b: int(a) - int(b), 1)
        self.assertEqual(counts, {'total': 3, 'security': 1, 'held': 1, 'security_held': 1})

    def test_newer_security_version_above_pinned_candidate_is_not_counted(self):
        packages = [package(version(1), version(2), [version(3, [origin()]), version(2)])]
        counts = apt_stats.count_updates(packages, lambda a, b: int(a) - int(b), 1)
        self.assertEqual(counts['total'], 1)
        self.assertEqual(counts['security'], 0)

    def test_distro_fields(self):
        with patch.object(system_stats.platform, 'freedesktop_os_release', return_value={
            'ID': 'debian', 'VERSION_ID': '13', 'VERSION_CODENAME': 'trixie'
        }):
            distro = system_stats.distribution()
        self.assertEqual(distro['id'], 'debian')
        self.assertEqual(distro['version_id'], '13')
        self.assertIsNone(distro['id_like'])

    def test_helper_timeout_and_failure_are_not_zero_updates(self):
        for error, status in ((subprocess.TimeoutExpired('helper', 10), 'unavailable'),
                              (FileNotFoundError(), 'unsupported'),
                              (PermissionError(), 'permission_denied'),
                              (subprocess.CalledProcessError(1, 'helper'), 'unavailable')):
            with self.subTest(error=error), patch.object(system_stats.subprocess, 'run', side_effect=error):
                result = system_stats.apt()
            self.assertEqual(result['updates']['status'], status)
            self.assertIsNone(result['updates']['data'])
            self.assertEqual(result['status'], 'partial')

    def test_helper_uses_isolated_system_python_and_deadline(self):
        with patch.object(system_stats.subprocess, 'run', return_value=NS(
            stdout='{"status":"ok","data":{"total":0,"security":0}}'
        )) as run:
            result = system_stats.apt()
        self.assertEqual(run.call_args.args[0][:2], ['/usr/bin/python3', '-I'])
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertEqual(result['updates']['data']['security'], 0)

    def test_missing_bindings(self):
        with patch.dict('sys.modules', {'apt': None}):
            result = apt_stats.collect()
        self.assertEqual(result['reason'], 'python3_apt_missing')

    def test_missing_indexes_are_not_zero_updates(self):
        fake_apt_pkg = NS(config=NS(find_dir=lambda key: '/nonexistent'))
        with patch.dict('sys.modules', {'apt': NS(), 'apt_pkg': fake_apt_pkg}), \
             patch.object(apt_stats.Path, 'iterdir', return_value=iter([])):
            result = apt_stats.collect()
        self.assertEqual(result['reason'], 'package_indexes_missing')
        self.assertIsNone(result['data'])
