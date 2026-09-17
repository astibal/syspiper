import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import SysPiper
from config_validation import validate_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api_key': 'test-key', 'allowed_nodes': {'hop': 'https://hop.invalid:8181/base'},
            'allowed_paths': {'status': '/api/~~version~~'},
            'parts': {'@version': {'hop': 'v1'}},
            'headers': {'hop': [['Authorization', 'Bearer test-key']]},
            'tls_verify': {'hop': True}, 'routes': {'target-*': 'hop'},
        }

    def test_valid_minimal_example_and_optional_settings(self):
        for config in ({'api_key': 'test'}, self.config,
                       json.loads(Path('config_example.json').read_text())):
            self.assertIs(validate_config(config), config)
        for ips in (None, [], ['::1', '192.0.2.1/24', '2001:db8::/32']):
            config = {**self.config, 'allowed_ips': ips, 'upstream_deadline_seconds': 0.5}
            validate_config(config)
        validate_config({**self.config, 'log_level': 'debug', 'tls_verify': {'hop': False}})

    def test_invalid_options_are_rejected_without_values(self):
        cases = [
            ('myip.url', 'secret-value'), ('unknown-secret-value', True),
            ('api_key', None), ('api_key', ''), ('api_key', 'x' * 257),
            ('api_key', 'secret-value\n'), ('api_key', '☃secret-value'),
            ('log_level', 'secret-value'), ('log_level', 1),
            ('allowed_ips', '127.0.0.1'), ('allowed_ips', ['secret-value']), ('allowed_ips', [1]),
            ('allowed_nodes', []), ('allowed_paths', None), ('parts', []),
            ('headers', []), ('tls_verify', None), ('routes', []),
            ('allowed_nodes', {'bad/name': 'https://hop.invalid'}),
            ('allowed_paths', {'bad/name': '/path'}),
            ('allowed_nodes', {'hop': 'file:///secret-value'}),
            ('allowed_nodes', {'hop': 'https://user:secret-value@hop.invalid'}),
            ('allowed_nodes', {'hop': 'https://hop.invalid:99999'}),
            ('allowed_nodes', {'hop': 'https://hop.invalid:bad'}),
            ('allowed_nodes', {'hop': 'https://hop.invalid?secret-value'}),
            ('allowed_nodes', {'hop': 'https://hop.invalid/#secret-value'}),
            ('allowed_nodes', {'hop': 'https://[broken'}),
            ('myip_url', '//hop.invalid'), ('myip_url', 42),
            ('parts', {'@version': {'absent': 'secret-value'}}),
            ('parts', {'@version': {'hop': 42}}),
            ('parts', {'version': {'hop': 'secret-value'}}),
            ('allowed_paths', {'status': '/~~undefined~~'}),
            ('allowed_paths', {'status': '/~~bad-name~~'}),
            ('allowed_paths', {'status': 'https://other.invalid/secret-value'}),
            ('allowed_paths', {'status': '//other.invalid/secret-value'}),
            ('allowed_paths', {'status': '/path#secret-value'}),
            ('allowed_paths', {'status': '/path\r\nsecret-value'}),
            ('headers', {'absent': []}), ('headers', {'hop': {'X-Key': 'secret-value'}}),
            ('headers', {'hop': [['X-Key']]}), ('headers', {'hop': [['X-Key', 123]]}),
            ('headers', {'hop': [['X-Key:', 'secret-value']]}),
            ('headers', {'hop': [['X-Key', 'secret-value\r\nInjected: yes']]}),
            ('headers', {'hop': [['X-Key', ' secret-value']]}),
            ('headers', {'hop': [['X-Key', '☃secret-value']]}),
            ('tls_verify', {'hop': 'false'}), ('tls_verify', {'absent': True}),
            ('routes', {'*': 'absent'}), ('routes', {'*': ['hop']}), ('routes', {'': 'hop'}),
        ]
        cases.extend(('upstream_deadline_seconds', value)
                     for value in (False, 0, -1, 301, 10 ** 1000, '15', None, float('inf'), float('nan')))
        for field, value in cases:
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config[field] = value
                with self.assertRaises(ValueError) as caught:
                    validate_config(config)
                self.assertNotIn('secret-value', str(caught.exception))

    def test_template_cannot_turn_relative_path_into_absolute_url(self):
        config = {**self.config, 'allowed_paths': {'status': '~~version~~'},
                  'parts': {'@version': {'hop': 'https://other.invalid/secret-value'}}}
        with self.assertRaises(ValueError) as caught:
            validate_config(config)
        self.assertNotIn('secret-value', str(caught.exception))

    def test_root_and_duplicate_keys_and_invalid_json_fail_at_startup(self):
        for text in ('[]', 'null', '"secret-value"', '{"api_key":"test","api_key":"secret-value"}',
                     '{"api_key":"test","headers":{"hop":[],"hop":[]}}',
                     '{"api_key":"secret-value",BROKEN}'):
            with self.subTest(text_type=text[:1]), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'config.json'
                config.write_text(text)
                with patch('app.Flask') as flask, self.assertRaises(ValueError) as caught:
                    SysPiper(config)
                flask.assert_not_called()
                self.assertNotIn('secret-value', str(caught.exception))

    def test_development_port_is_8181(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'api_key': 'test'}))
            service = SysPiper(config)
            with patch.object(service.app, 'run') as run:
                service.run()
            run.assert_called_once_with(host='0.0.0.0', port=8181)
