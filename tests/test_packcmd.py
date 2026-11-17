import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar import packcmd

HAS_GIT = bool(shutil.which('git'))


def make_pack(d, name='demo', requires='0.1', body='def register(api): pass\n'):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'pack.json'), 'w') as f: json.dump({'name': name, 'version': '1.0', 'requires_core': requires}, f)
    with open(os.path.join(d, 'pack.py'), 'w') as f: f.write(body)


def git_repo(d):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    for c in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'x']): subprocess.run(['git', '-C', d] + c, check=True, env=env)
    return env


class PackCmd(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(); self.home = os.path.join(self.t, 'home'); os.makedirs(self.home)
        with open(os.path.join(self.home, 'worker.json'), 'w') as f: json.dump({'model': 'm', 'packs': []}, f)

    def tearDown(self): shutil.rmtree(self.t)

    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): rc = packcmd.main(['--home', self.home] + list(argv))
        return rc, out.getvalue()

    def cfg(self):
        with open(os.path.join(self.home, 'worker.json')) as f: return json.load(f)

    @unittest.skipUnless(HAS_GIT, 'git not installed')
    def test_install_from_git_enables_and_keeps_other_settings(self):
        src = os.path.join(self.t, 'lamellar-pack-demo'); make_pack(src); git_repo(src)
        rc, out = self.run_cmd('install', src)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.home, 'packs', 'demo', 'pack.json')))
        c = self.cfg(); self.assertEqual(c['packs'], ['demo']); self.assertEqual(c['model'], 'm')
        self.assertIn(os.path.join(self.home, 'packs'), c['packs_dir'])
        rc, out = self.run_cmd('install', src); self.assertEqual(rc, 1); self.assertIn('already installed', out)

    @unittest.skipUnless(HAS_GIT, 'git not installed')
    def test_update_pulls(self):
        src = os.path.join(self.t, 'demo'); make_pack(src); env = git_repo(src)
        self.run_cmd('install', src)
        with open(os.path.join(src, 'NEW'), 'w') as f: f.write('x')
        subprocess.run(['git', '-C', src, 'add', '-A'], check=True, env=env); subprocess.run(['git', '-C', src, 'commit', '-qm', 'y'], check=True, env=env)
        rc, out = self.run_cmd('update', 'demo'); self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.home, 'packs', 'demo', 'NEW')))

    def test_not_a_pack_leaves_nothing(self):
        src = os.path.join(self.t, 'junk'); os.makedirs(src)
        rc, out = self.run_cmd('install', src)
        self.assertEqual(rc, 1); self.assertIn('not a lamellar pack', out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'packs', 'junk'))); self.assertEqual(self.cfg()['packs'], [])

    def test_too_new_core_refused(self):
        src = os.path.join(self.t, 'future'); make_pack(src, 'future', requires='99.0')
        rc, out = self.run_cmd('install', src)
        self.assertEqual(rc, 1); self.assertIn('needs lamellar 99.0', out); self.assertFalse(os.path.exists(os.path.join(self.home, 'packs', 'future')))

    def test_broken_pack_installed_but_not_enabled(self):
        src = os.path.join(self.t, 'broken'); make_pack(src, 'broken', body='def register(api): raise RuntimeError("boom")\n')
        rc, out = self.run_cmd('install', src)
        self.assertEqual(rc, 1); self.assertIn('NOT enabled', out); self.assertIn('boom', out)
        self.assertEqual(self.cfg()['packs'], [])

    def test_enable_disable_list_remove(self):
        src = os.path.join(self.t, 'demo'); make_pack(src)
        rc, _ = self.run_cmd('install', src, '--no-enable'); self.assertEqual(rc, 0); self.assertEqual(self.cfg()['packs'], [])
        self.assertEqual(self.run_cmd('enable', 'demo')[0], 0); self.assertEqual(self.cfg()['packs'], ['demo'])
        self.assertIn('on   demo 1.0', self.run_cmd('list')[1])
        self.assertEqual(self.run_cmd('disable', 'demo')[0], 0); self.assertEqual(self.cfg()['packs'], [])
        self.assertEqual(self.run_cmd('remove', 'demo')[0], 0); self.assertFalse(os.path.exists(os.path.join(self.home, 'packs', 'demo')))

    def test_bad_names_refused(self):
        src = os.path.join(self.t, 'demo'); make_pack(src)
        self.assertEqual(self.run_cmd('install', src, '--name', '../evil')[0], 1)
        self.assertEqual(self.run_cmd('remove', '../packs')[0], 1)
        self.assertTrue(os.path.isdir(self.home))

    def test_name_from_url(self):
        self.assertEqual(packcmd._name_from('https://github.com/x/lamellar-pack-kal.git'), 'kal')
        self.assertEqual(packcmd._name_from('git@github.com:x/kal.git'), 'kal')
        self.assertEqual(packcmd._name_from('/tmp/foo/'), 'foo')


if __name__ == '__main__':
    unittest.main()
