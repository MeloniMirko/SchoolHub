import os, sys, tempfile, shutil, subprocess, hashlib, unittest
from pathlib import Path
from unittest import mock

TEST_APPDATA = tempfile.mkdtemp(prefix='schoolhub-tests-appdata-')
os.environ['LOCALAPPDATA'] = TEST_APPDATA
SRC = str(Path(__file__).resolve().parents[1] / 'src')
sys.path.insert(0, SRC)

import workspace as workspace_mod
from workspace import WorkspaceManager, WorkspaceError
import schoolhub_gui
from schoolhub_gui import SchoolHub

PASSWORD = 'Password-Test-123!'

def tree_bytes_hash(root):
    root = Path(root)
    h = hashlib.sha256()
    if not root.exists(): return h.hexdigest()
    for p in sorted([x for x in root.rglob('*') if x.is_file()], key=lambda x: str(x)):
        h.update(str(p.relative_to(root)).encode()); h.update(b'\0'); h.update(p.read_bytes()); h.update(b'\0')
    return h.hexdigest()

def run(cmd, cwd=None):
    cp = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if cp.returncode != 0:
        raise AssertionError(f"command failed {cmd}\nOUT={cp.stdout}\nERR={cp.stderr}")
    return cp.stdout.strip()

def git_seed_remote(remote, files):
    work = tempfile.mkdtemp(prefix='schoolhub-seed-')
    try:
        run(['git','init','-b','master'], work)
        run(['git','config','user.name','Tester'], work)
        run(['git','config','user.email','tester@example.invalid'], work)
        for rel, content in files.items():
            p=Path(work)/rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, encoding='utf-8')
        run(['git','add','-A'], work)
        run(['git','commit','-m','seed'], work)
        run(['git','remote','add','origin',str(remote)], work)
        run(['git','push','-u','origin','master'], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

def git_remote_change(remote, rel, content=None, delete=False):
    work = tempfile.mkdtemp(prefix='schoolhub-remote-change-')
    try:
        run(['git','clone','-b','master',str(remote),work])
        run(['git','config','user.name','Remote Tester'], work)
        run(['git','config','user.email','remote@example.invalid'], work)
        p=Path(work)/rel
        if delete:
            if p.is_dir(): shutil.rmtree(p)
            elif p.exists(): p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, encoding='utf-8')
        run(['git','add','-A'], work)
        out = subprocess.run(['git','commit','-m','remote change'],cwd=work,text=True,capture_output=True)
        if out.returncode != 0 and 'nothing to commit' not in (out.stdout+out.stderr).lower():
            raise AssertionError(out.stderr)
        run(['git','push','origin','master'], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

def read_remote_file(remote, rel):
    work=tempfile.mkdtemp(prefix='schoolhub-read-')
    try:
        run(['git','clone','-b','master',str(remote),work])
        p=Path(work)/rel
        return p.read_text(encoding='utf-8') if p.exists() else None
    finally:
        shutil.rmtree(work, ignore_errors=True)

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix='schoolhub-ws-'))
        self.wp = self.td/'Workspaces'/'Scuola'
        self.vp = self.td/'Vaults'/'Scuola.vault'
        self.wp.mkdir(parents=True)
        (self.wp/'nested').mkdir()
        (self.wp/'a.txt').write_text('A0',encoding='utf-8')
        (self.wp/'nested'/'b.bin').write_bytes(b'\x00\x01abc')
        self.wm = WorkspaceManager(self.wp,self.vp)
    def tearDown(self): shutil.rmtree(self.td, ignore_errors=True)

    def test_create_unlock_modify_lock_unlock_cycle(self):
        self.wm.create(PASSWORD)
        self.assertTrue(self.wm.exists)
        self.assertEqual(list(self.wp.rglob('*')), [])
        self.wm.unlock(PASSWORD)
        self.assertEqual((self.wp/'a.txt').read_text(), 'A0')
        (self.wp/'a.txt').write_text('A1',encoding='utf-8')
        (self.wp/'new.txt').write_text('NEW',encoding='utf-8')
        (self.wp/'nested'/'b.bin').unlink()
        self.wm.lock(PASSWORD)
        self.assertFalse(self.wm.is_unlocked)
        self.assertIsNone(self.wm._session_key)
        self.assertEqual(list(self.wp.rglob('*')), [])
        self.wm.unlock(PASSWORD)
        self.assertEqual((self.wp/'a.txt').read_text(), 'A1')
        self.assertEqual((self.wp/'new.txt').read_text(), 'NEW')
        self.assertFalse((self.wp/'nested'/'b.bin').exists())

    def test_wrong_password_never_changes_vault(self):
        self.wm.create(PASSWORD)
        before=tree_bytes_hash(self.vp)
        with self.assertRaises(WorkspaceError): self.wm.unlock('wrong-password')
        self.assertEqual(before, tree_bytes_hash(self.vp))
        self.assertFalse(self.wm.is_unlocked)

    def test_edit_during_lock_aborts_without_data_loss(self):
        self.wm.create(PASSWORD); self.wm.unlock(PASSWORD)
        (self.wp/'a.txt').write_text('PRELOCK', encoding='utf-8')
        original_encrypt=self.wm._encrypt_file
        touched={'done':False}
        def mutate_during_encrypt(source,dest,key,rel):
            original_encrypt(source,dest,key,rel)
            if not touched['done']:
                touched['done']=True
                (self.wp/'a.txt').write_text('EDIT-DURING-LOCK', encoding='utf-8')
        with mock.patch.object(self.wm,'_encrypt_file',side_effect=mutate_during_encrypt):
            with self.assertRaises(WorkspaceError):
                self.wm.lock(PASSWORD)
        self.assertTrue(self.wm.is_unlocked)
        self.assertEqual((self.wp/'a.txt').read_text(), 'EDIT-DURING-LOCK')
        # A clean retry must persist the newest bytes.
        self.wm.lock(PASSWORD); self.wm.unlock(PASSWORD)
        self.assertEqual((self.wp/'a.txt').read_text(), 'EDIT-DURING-LOCK')

    def test_create_cleanup_failure_keeps_vault_and_full_plaintext(self):
        original_replace = workspace_mod.os.replace
        def guarded(src,dst,*a,**k):
            if Path(src)==self.wp and '.creating-cleanup-' in str(dst):
                raise PermissionError('simulated Windows sharing violation')
            return original_replace(src,dst,*a,**k)
        with mock.patch.object(workspace_mod.os,'replace',side_effect=guarded):
            with self.assertRaises(WorkspaceError): self.wm.create(PASSWORD)
        self.assertTrue(self.wm.exists)
        self.assertEqual((self.wp/'a.txt').read_text(),'A0')
        self.assertEqual((self.wp/'nested'/'b.bin').read_bytes(),b'\x00\x01abc')

    def test_lock_cleanup_failure_is_retry_safe(self):
        self.wm.create(PASSWORD); self.wm.unlock(PASSWORD)
        (self.wp/'a.txt').write_text('LATEST',encoding='utf-8')
        original_replace = workspace_mod.os.replace
        def guarded(src,dst,*a,**k):
            if Path(src)==self.wp and '.locking-cleanup-' in str(dst):
                raise PermissionError('simulated open file')
            return original_replace(src,dst,*a,**k)
        with mock.patch.object(workspace_mod.os,'replace',side_effect=guarded):
            with self.assertRaises(WorkspaceError): self.wm.lock(PASSWORD)
        self.assertTrue(self.wm.is_unlocked)
        self.assertEqual((self.wp/'a.txt').read_text(),'LATEST')
        # A normal retry must not lose files.
        self.wm.lock(PASSWORD)
        self.wm.unlock(PASSWORD)
        self.assertEqual((self.wp/'a.txt').read_text(),'LATEST')
        self.assertTrue((self.wp/'nested'/'b.bin').exists())

    def test_import_export_and_save_unlocked(self):
        self.wm.create(PASSWORD)
        src=self.td/'incoming'; src.mkdir(); (src/'x.txt').write_text('X',encoding='utf-8')
        self.wm.import_from(src,PASSWORD)
        out=self.td/'out'; self.wm.export_to(out,PASSWORD)
        self.assertEqual((out/'x.txt').read_text(),'X')
        self.wm.unlock(PASSWORD)
        final=self.td/'final'; final.mkdir(); (final/'y.txt').write_text('Y',encoding='utf-8')
        self.wm.replace_plaintext_from_tree(final)
        self.wm.save_unlocked_to_vault(final)
        verify=self.td/'verify'; self.wm.export_to(verify,PASSWORD)
        self.assertEqual((verify/'y.txt').read_text(),'Y')
        self.assertFalse((verify/'x.txt').exists())

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.td=Path(tempfile.mkdtemp(prefix='schoolhub-sync-test-'))
        self.remote=self.td/'remote.git'
        run(['git','init','--bare',str(self.remote)])
        self.wp=self.td/'Workspaces'/'Scuola'; self.wp.mkdir(parents=True)
        self.vp=self.td/'Vaults'/'Scuola.vault'
        self.wm=WorkspaceManager(self.wp,self.vp)
        self.wm.create(PASSWORD)
        self.state=self.td/'SyncState'/'Scuola.json'
        self.app=SchoolHub.__new__(SchoolHub)
        self.app.workspace=self.wm
        self.app.remote=str(self.remote)
        self.app.branch='master'
        self.app.github_user='tester'
        self.app.sync_state_file=str(self.state)
        self.app.last_conflicts=[]
        self.app.sync_running=True
        self.app.running=False
        self.app.write_log=lambda *a,**k: None
        self.app.finish_status=lambda *a,**k: None
    def tearDown(self): shutil.rmtree(self.td, ignore_errors=True)

    def test_locked_first_sync_remote_only_then_bidirectional(self):
        git_seed_remote(self.remote, {'remote.txt':'R0','same.txt':'S0'})
        self.app.sync_worker(PASSWORD)
        out=self.td/'check1'; self.wm.export_to(out,PASSWORD)
        self.assertEqual((out/'remote.txt').read_text(),'R0')
        self.assertFalse(self.wm.is_unlocked)
        self.assertTrue(self.state.exists())

        self.wm.unlock(PASSWORD)
        (self.wp/'local.txt').write_text('L1',encoding='utf-8')
        self.app.sync_worker(None)
        self.assertEqual(read_remote_file(self.remote,'local.txt'),'L1')

        git_remote_change(self.remote,'remote.txt','R1')
        self.app.sync_worker(None)
        self.assertEqual((self.wp/'remote.txt').read_text(),'R1')

    def test_independent_changes_merge(self):
        git_seed_remote(self.remote, {'base.txt':'B'})
        self.wm.unlock(PASSWORD)
        self.app.sync_worker(None)
        (self.wp/'local.txt').write_text('LOCAL',encoding='utf-8')
        git_remote_change(self.remote,'remote.txt','REMOTE')
        self.app.sync_worker(None)
        self.assertEqual((self.wp/'remote.txt').read_text(),'REMOTE')
        self.assertEqual(read_remote_file(self.remote,'local.txt'),'LOCAL')
        self.assertEqual(read_remote_file(self.remote,'remote.txt'),'REMOTE')
        self.assertEqual(self.app.last_conflicts,[])

    def test_conflict_detect_and_resolve_both_directions(self):
        git_seed_remote(self.remote, {'same.txt':'BASE'})
        self.wm.unlock(PASSWORD)
        self.app.sync_worker(None)
        (self.wp/'same.txt').write_text('LOCAL1',encoding='utf-8')
        git_remote_change(self.remote,'same.txt','REMOTE1')
        self.app.sync_worker(None)
        self.assertEqual(self.app.last_conflicts,['same.txt'])
        self.assertEqual((self.wp/'same.txt').read_text(),'LOCAL1')
        self.assertEqual(read_remote_file(self.remote,'same.txt'),'REMOTE1')
        self.app.sync_running=True
        self.app._resolve_conflict_worker('local',None)
        self.assertEqual(read_remote_file(self.remote,'same.txt'),'LOCAL1')
        self.assertEqual(self.app.last_conflicts,[])

        (self.wp/'same.txt').write_text('LOCAL2',encoding='utf-8')
        git_remote_change(self.remote,'same.txt','REMOTE2')
        self.app.sync_worker(None)
        self.assertEqual(self.app.last_conflicts,['same.txt'])
        self.app.sync_running=True
        self.app._resolve_conflict_worker('remote',None)
        self.assertEqual((self.wp/'same.txt').read_text(),'REMOTE2')
        self.assertEqual(read_remote_file(self.remote,'same.txt'),'REMOTE2')

    def test_empty_remote_local_first_push(self):
        # Recreate Vault with a local file before first sync.
        self.wm.unlock(PASSWORD)
        (self.wp/'first.txt').write_text('FIRST',encoding='utf-8')
        self.app.sync_worker(None)
        self.assertEqual(read_remote_file(self.remote,'first.txt'),'FIRST')

    def test_file_directory_shape_change(self):
        git_seed_remote(self.remote, {'thing/child.txt':'old'})
        self.wm.unlock(PASSWORD); self.app.sync_worker(None)
        shutil.rmtree(self.wp/'thing')
        (self.wp/'thing').write_text('now-file',encoding='utf-8')
        self.app.sync_worker(None)
        self.assertEqual(read_remote_file(self.remote,'thing'),'now-file')

    def test_same_deletion_on_both_sides_advances_baseline(self):
        git_seed_remote(self.remote, {'same.txt':'BASE'})
        self.wm.unlock(PASSWORD); self.app.sync_worker(None)
        baseline_before=self.app._load_sync_state()['files']
        self.assertIn('same.txt', baseline_before)
        (self.wp/'same.txt').unlink()
        git_remote_change(self.remote,'same.txt',delete=True)
        self.app.sync_worker(None)
        baseline_after=self.app._load_sync_state()['files']
        self.assertNotIn('same.txt', baseline_after)
        self.assertEqual(self.app.last_conflicts,[])

    def test_symlink_in_sync_tree_is_rejected(self):
        target=self.td/'outside.txt'; target.write_text('secret',encoding='utf-8')
        link=self.wp/'link.txt'
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlink unavailable')
        with self.assertRaises(WorkspaceError):
            self.app._hash_tree(self.wp)

    def test_edit_during_sync_aborts_and_preserves_live_edit(self):
        git_seed_remote(self.remote, {'base.txt':'BASE'})
        self.wm.unlock(PASSWORD); self.app.sync_worker(None)
        state_before=self.state.read_bytes()
        real_clone=self.app._clone_remote
        def clone_then_edit(destination):
            real_clone(destination)
            (self.wp/'base.txt').write_text('EDIT-WHILE-SYNCING', encoding='utf-8')
        with mock.patch.object(self.app,'_clone_remote',side_effect=clone_then_edit):
            self.app.sync_worker(None)
        self.assertEqual((self.wp/'base.txt').read_text(), 'EDIT-WHILE-SYNCING')
        self.assertEqual(read_remote_file(self.remote,'base.txt'),'BASE')
        self.assertEqual(self.state.read_bytes(), state_before)
        # Next clean sync should upload the preserved edit.
        self.app.sync_worker(None)
        self.assertEqual(read_remote_file(self.remote,'base.txt'),'EDIT-WHILE-SYNCING')

class MigrationTests(unittest.TestCase):
    def test_old_workspace_and_vault_are_migrated_to_scoula_paths(self):
        td=Path(tempfile.mkdtemp(prefix='schoolhub-migration-'))
        try:
            old_wp=td/'Workspace'; old_wp.mkdir(parents=True); (old_wp/'legacy.txt').write_text('legacy',encoding='utf-8')
            old_vp=td/'Workspace.vault'
            wm=WorkspaceManager(old_wp,old_vp); wm.create(PASSWORD)
            # Recreate a plaintext legacy Workspace to verify its path migration too.
            wm.unlock(PASSWORD); (old_wp/'legacy.txt').write_text('legacy2',encoding='utf-8')
            wm.save_unlocked_to_vault(old_wp)
            old_state=td/'sync_state.json'; old_state.write_text('{"version":1,"files":{}}',encoding='utf-8')
            app=SchoolHub.__new__(SchoolHub)
            app.config={
                'interval':300,'auto_sync':True,'auto_start':True,
                'workspace_path':str(old_wp),'vault_path':str(old_vp),
                'remote':'https://github.com/MeloniMirko/Scuola.git','branch':'master',
                'sync_state_file':str(old_state)
            }
            with mock.patch.object(schoolhub_gui,'APP_DIR',str(td)), \
                 mock.patch.object(schoolhub_gui,'DEFAULT_WORKSPACE',str(td/'Workspaces'/'Scuola')), \
                 mock.patch.object(schoolhub_gui,'CONFIG_FILE',str(td/'config.json')):
                app._ensure_vault_config()
            self.assertTrue((td/'Vaults'/'Scuola.vault'/'vault.json').exists())
            self.assertEqual(Path(app.config['workspace_path']), td/'Workspaces'/'Scuola')
            self.assertEqual(Path(app.config['vault_path']), td/'Vaults'/'Scuola.vault')
            self.assertTrue((td/'SyncState'/'Scuola.json').exists())
        finally:
            shutil.rmtree(td,ignore_errors=True)

class StaticTests(unittest.TestCase):
    def test_all_self_called_methods_exist(self):
        import ast, inspect
        tree=ast.parse(Path(schoolhub_gui.__file__).read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SchoolHub')
        defs={n.name for n in cls.body if isinstance(n,ast.FunctionDef)}
        calls=set()
        for n in ast.walk(cls):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id=='self':
                calls.add(n.func.attr)
        self.assertEqual(sorted(calls-defs),[])

if __name__=='__main__':
    try: unittest.main(verbosity=2)
    finally: shutil.rmtree(TEST_APPDATA, ignore_errors=True)
