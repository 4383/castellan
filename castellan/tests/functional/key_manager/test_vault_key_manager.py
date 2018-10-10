#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Functional test cases for the Vault key manager.

Note: This requires local running instance of Vault.
"""
import abc
import os
import uuid

from oslo_config import cfg
from oslo_context import context
from oslo_utils import uuidutils
from oslotest import base
import requests
from testtools import testcase

from castellan.common import exception
from castellan.key_manager import vault_key_manager
from castellan.tests.functional import config
from castellan.tests.functional.key_manager import test_key_manager

CONF = config.get_config()


class VaultKeyManagerTestCase(test_key_manager.KeyManagerTestCase):
    def _create_key_manager(self):
        key_mgr = vault_key_manager.VaultKeyManager(cfg.CONF)

        if ('VAULT_TEST_URL' not in os.environ or
                'VAULT_TEST_ROOT_TOKEN' not in os.environ):
            raise testcase.TestSkipped('Missing Vault setup information')

        key_mgr._root_token_id = os.environ['VAULT_TEST_ROOT_TOKEN']
        key_mgr._vault_url = os.environ['VAULT_TEST_URL']
        return key_mgr

    @abc.abstractmethod
    def get_context(self):
        """Retrieves Context for Authentication"""
        return

    def setUp(self):
        super(VaultKeyManagerTestCase, self).setUp()
        self.ctxt = self.get_context()

    def tearDown(self):
        super(VaultKeyManagerTestCase, self).tearDown()

    def test_create_null_context(self):
        self.assertRaises(exception.Forbidden,
                          self.key_mgr.create_key, None, 'AES', 256)

    def test_create_key_pair_null_context(self):
        self.assertRaises(exception.Forbidden,
                          self.key_mgr.create_key_pair, None, 'RSA', 2048)

    def test_create_key_pair_bad_algorithm(self):
        self.assertRaises(
            NotImplementedError,
            self.key_mgr.create_key_pair,
            self.ctxt, 'DSA', 2048
        )

    def test_delete_null_context(self):
        key_uuid = self._get_valid_object_uuid(
            test_key_manager._get_test_symmetric_key())
        self.addCleanup(self.key_mgr.delete, self.ctxt, key_uuid)
        self.assertRaises(exception.Forbidden,
                          self.key_mgr.delete, None, key_uuid)

    def test_delete_null_object(self):
        self.assertRaises(exception.KeyManagerError,
                          self.key_mgr.delete, self.ctxt, None)

    def test_get_null_context(self):
        key_uuid = self._get_valid_object_uuid(
            test_key_manager._get_test_symmetric_key())
        self.addCleanup(self.key_mgr.delete, self.ctxt, key_uuid)
        self.assertRaises(exception.Forbidden,
                          self.key_mgr.get, None, key_uuid)

    def test_get_null_object(self):
        self.assertRaises(exception.KeyManagerError,
                          self.key_mgr.get, self.ctxt, None)

    def test_get_unknown_key(self):
        bad_key_uuid = uuidutils.generate_uuid()
        self.assertRaises(exception.ManagedObjectNotFoundError,
                          self.key_mgr.get, self.ctxt, bad_key_uuid)

    def test_store_null_context(self):
        key = test_key_manager._get_test_symmetric_key()

        self.assertRaises(exception.Forbidden,
                          self.key_mgr.store, None, key)


class VaultKeyManagerOSLOContextTestCase(VaultKeyManagerTestCase,
                                         base.BaseTestCase):
    def get_context(self):
        return context.get_admin_context()


TEST_POLICY = '''
path "{backend}/*" {{
  capabilities = ["create", "read", "update", "delete", "list"]
}}

path "sys/internal/ui/mounts/{backend}" {{
  capabilities = ["read"]
}}
'''

AUTH_ENDPOINT = 'v1/sys/auth/{auth_type}'
POLICY_ENDPOINT = 'v1/sys/policy/{policy_name}'
APPROLE_ENDPOINT = 'v1/auth/approle/role/{role_name}'


class VaultKeyManagerAppRoleTestCase(VaultKeyManagerOSLOContextTestCase):

    def _create_key_manager(self):
        key_mgr = vault_key_manager.VaultKeyManager(cfg.CONF)

        if ('VAULT_TEST_URL' not in os.environ or
                'VAULT_TEST_ROOT_TOKEN' not in os.environ):
            raise testcase.TestSkipped('Missing Vault setup information')

        self.root_token_id = os.environ['VAULT_TEST_ROOT_TOKEN']
        self.vault_url = os.environ['VAULT_TEST_URL']

        test_uuid = str(uuid.uuid4())
        vault_policy = 'policy-{}'.format(test_uuid)
        vault_approle = 'approle-{}'.format(test_uuid)

        self.session = requests.Session()
        self.session.headers.update({'X-Vault-Token': self.root_token_id})

        self._enable_approle()
        self._create_policy(vault_policy)
        self._create_approle(vault_approle, vault_policy)

        key_mgr._approle_role_id, key_mgr._approle_secret_id = (
            self._retrieve_approle(vault_approle)
        )
        key_mgr._vault_url = self.vault_url
        return key_mgr

    def _enable_approle(self):
        params = {
            'type': 'approle'
        }
        self.session.post(
            '{}/{}'.format(
                self.vault_url,
                AUTH_ENDPOINT.format(auth_type='approle')
            ),
            json=params,
        )

    def _create_policy(self, vault_policy):
        params = {
            'rules': TEST_POLICY.format(backend='secret'),
        }
        self.session.put(
            '{}/{}'.format(
                self.vault_url,
                POLICY_ENDPOINT.format(policy_name=vault_policy)
            ),
            json=params,
        )

    def _create_approle(self, vault_approle, vault_policy):
        params = {
            'token_ttl': '60s',
            'token_max_ttl': '60s',
            'policies': [vault_policy],
            'bind_secret_id': 'true',
            'bound_cidr_list': '127.0.0.1/32'
        }
        self.session.post(
            '{}/{}'.format(
                self.vault_url,
                APPROLE_ENDPOINT.format(role_name=vault_approle)
            ),
            json=params,
        )

    def _retrieve_approle(self, vault_approle):
        approle_role_id = (
            self.session.get(
                '{}/v1/auth/approle/role/{}/role-id'.format(
                    self.vault_url,
                    vault_approle
                )).json()['data']['role_id']
        )
        approle_secret_id = (
            self.session.post(
                '{}/v1/auth/approle/role/{}/secret-id'.format(
                    self.vault_url,
                    vault_approle
                )).json()['data']['secret_id']
        )
        return (approle_role_id, approle_secret_id)
