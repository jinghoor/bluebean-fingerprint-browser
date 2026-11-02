"""Multi-account storage checks use only temporary profiles."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from environment_accounts import AccountStore, catalog, totp
from local_account_vault import LocalAccountVault

class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bluebean-accounts-test-')
        root=Path(self.tmp.name)
        self.legacy=LocalAccountVault(root/'legacy.json')
        self.store=AccountStore(self.legacy)
        self.patch=patch('local_account_vault._dpapi',side_effect=lambda value,protect:value)
        self.patch.start()
    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()
    def save(self,client,**more):
        return self.store.save('env-1',{'client_id':client,'platform_id':'facebook','login':client+'@example.test',**more})
    def test_platform_directory_and_multi_account_isolation(self):
        ids={p['id'] for p in catalog()}
        self.assertTrue({'facebook','instagram','amazon','shopee','custom'}<=ids)
        first=self.save('one')
        second=self.save('two')
        self.assertNotEqual(first['account_id'],second['account_id'])
        self.assertNotEqual(first['session_env_id'],second['session_env_id'])
        self.assertEqual(len(self.store.list('env-1')),2)
        self.assertNotIn('password',self.store.list('env-1')[0])
    def test_identity_change_requires_confirmation_and_drops_old_secrets(self):
        row=self.save('one',password='secret',password_action='replace',otp_secret='JBSWY3DPEHPK3PXP',otp_secret_action='replace')
        with self.assertRaisesRegex(ValueError,'更换账号或平台需确认'):
            self.store.save('env-1',{'account_id':row['account_id'],'revision':row['revision'],'platform_id':'amazon','login':'other@example.test'})
        changed=self.store.save('env-1',{'account_id':row['account_id'],'revision':row['revision'],'platform_id':'amazon','login':'other@example.test','confirm_identity_change':True})
        self.assertFalse(changed['has_password']);self.assertFalse(changed['has_otp'])
        self.assertNotEqual(changed['session_env_id'],row['session_env_id'])
    def test_legacy_password_and_otp_are_adopted_without_modifying_source(self):
        self.legacy.save('env-1',{'platform':'Facebook','username':'old-login','password':'old-secret','otp_secret':'JBSWY3DPEHPK3PXP'})
        original=self.legacy.path.read_bytes()
        rows=self.store.list('env-1')
        self.assertEqual(rows[0]['login'],'old-login');self.assertTrue(rows[0]['has_password']);self.assertTrue(rows[0]['has_otp'])
        self.assertEqual(self.legacy.path.read_bytes(),original)
    def test_listing_uses_plain_summary_without_decrypting_credentials(self):
        row=self.save('summary',password='secret-summary',password_action='replace')
        with patch('environment_accounts._dpapi',side_effect=AssertionError('list decrypted a credential')):
            public=self.store.list('env-1')
        self.assertTrue(public[0]['has_password'])
        self.assertEqual(public[0]['account_id'],row['account_id'])
    def test_partial_batch_preserves_each_result_and_existing_account(self):
        reply=self.store.save_many('env-1',[{'client_id':'a','platform_id':'facebook','login':'a@example.test'},
            {'client_id':'bad','platform_id':'unknown','login':'bad@example.test'},
            {'client_id':'b','platform_id':'amazon','login':'b@example.test'}])
        self.assertFalse(reply['ok']);self.assertEqual([x['ok'] for x in reply['results']],[True,False,True])
        self.assertEqual(len(self.store.list('env-1')),2)
    def test_password_and_totp_can_be_cleared_independently(self):
        row=self.save('credentials',password='pw-secret',password_action='replace',
                      otp_secret='JBSWY3DPEHPK3PXP',otp_secret_action='replace')
        updated=self.store.save('env-1',{'account_id':row['account_id'],'revision':row['revision'],
            'password_action':'clear','otp_secret_action':'keep'})
        self.assertFalse(updated['has_password']);self.assertTrue(updated['has_otp'])
        updated=self.store.save('env-1',{'account_id':row['account_id'],'revision':updated['revision'],
            'password_action':'keep','otp_secret_action':'clear'})
        self.assertFalse(updated['has_password']);self.assertFalse(updated['has_otp'])
    def test_same_identity_edit_keeps_dedicated_session_and_credentials(self):
        row=self.save('stable',password='pw-secret',password_action='replace')
        updated=self.store.save('env-1',{'account_id':row['account_id'],'revision':row['revision'],
            'label':'updated note','password_action':'keep','otp_secret_action':'keep'})
        stored=self.store.get('env-1',row['account_id'])
        self.assertEqual(updated['session_env_id'],row['session_env_id'])
        self.assertEqual(stored['password'],'pw-secret')
        self.assertEqual(updated['label'],'updated note')
    def test_totp_rfc6238_sha1_vector(self):
        secret=base64.b32encode(b'12345678901234567890').decode().rstrip('=')
        result=totp(secret,now=59)
        self.assertEqual(result['code'],'287082')
        self.assertEqual(result['remaining'],1)

if __name__=='__main__': unittest.main()
