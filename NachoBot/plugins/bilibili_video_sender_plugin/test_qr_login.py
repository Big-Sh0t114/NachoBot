import unittest,tempfile,contextlib,io
from pathlib import Path
from unittest.mock import Mock,patch
import requests
import qr_login as q
class LoginTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'config.toml'
  self.old={'bilibili':{'sessdata':'old-test','buvid3':'old-device'}}
  q.save_config(self.p,self.old);self.original=self.p.read_bytes()
  self.s=requests.Session()
  for name,value in zip(q.PRIMARY_COOKIE_NAMES,('fake-test-session','fake-test-csrf','123')):
   self.s.cookies.set(name,value,domain='.bilibili.com',path='/')
 def tearDown(self):self.tmp.cleanup()
 def run_case(self,payload):
  response=Mock(status_code=200);response.json.return_value=payload
  with patch.object(self.s,'get',return_value=response) as get,patch.object(q,'fetch_buvid',return_value={'buvid3':'fresh-test-device'}),contextlib.redirect_stdout(io.StringIO()) as output:
   ok=q.validate_and_save(self.s,self.p,self.old)
  if get.called:self.assertFalse(get.call_args.kwargs['allow_redirects'])
  self.assertNotIn('fake-test-session',output.getvalue())
  return ok
 def test_success(self):
  self.assertTrue(self.run_case({'code':0,'data':{'isLogin':True,'mid':123}}))
  new=q.load_config(self.p)['bilibili'];self.assertEqual(new['sessdata'],'fake-test-session');self.assertEqual(new['buvid3'],'fresh-test-device')
  self.assertEqual(self.old['bilibili']['sessdata'],'old-test')
 def test_rejected_preserves_old(self):
  self.assertFalse(self.run_case({'code':-101,'data':{'isLogin':False}}));self.assertEqual(self.p.read_bytes(),self.original)
 def test_account_mismatch_preserves_old(self):
  self.assertFalse(self.run_case({'code':0,'data':{'isLogin':True,'mid':456}}));self.assertEqual(self.p.read_bytes(),self.original)
 def test_missing_cookie_preserves_old(self):
  self.s.cookies.clear();self.assertFalse(self.run_case({}));self.assertEqual(self.p.read_bytes(),self.original)
 def test_passport_cookie_not_accepted(self):
  self.s.cookies.clear();self.s.cookies.set('SESSDATA','fake',domain='passport.bilibili.com',path='/');self.assertIsNone(q.extract_cookie(self.s,'SESSDATA'))
 def test_conflicting_cookie_rejected(self):
  self.s.cookies.set('SESSDATA','other-fake',domain='api.bilibili.com',path='/')
  self.assertFalse(self.run_case({}));self.assertEqual(self.p.read_bytes(),self.original)
 def test_http_redirect_rejected(self):
  with patch.object(self.s,'get',return_value=Mock(status_code=302)),contextlib.redirect_stdout(io.StringIO()):
   self.assertFalse(q.validate_and_save(self.s,self.p,self.old))
  self.assertEqual(self.p.read_bytes(),self.original)
 def test_network_failure_preserves_old(self):
  with patch.object(self.s,'get',side_effect=requests.RequestException('fake-secret-url')),contextlib.redirect_stdout(io.StringIO()) as output:
   self.assertFalse(q.validate_and_save(self.s,self.p,self.old))
  self.assertNotIn('fake-secret-url',output.getvalue());self.assertEqual(self.p.read_bytes(),self.original)
if __name__=='__main__':unittest.main()
