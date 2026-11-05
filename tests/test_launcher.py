import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import platform_support


@unittest.skipUnless(shutil.which('git'), 'Git required for update tests')
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=Path(tempfile.mkdtemp(prefix='sd-update-'))
        self.remote=self.temp/'remote.git';self.seed=self.temp/'seed';self.checkout=self.temp/'checkout'
        self.run_git(self.temp,'init','--bare','--initial-branch=main',str(self.remote))
        self.run_git(self.temp,'init','--initial-branch=main',str(self.seed))
        self.run_git(self.seed,'config','user.name','Test')
        self.run_git(self.seed,'config','user.email','test@example.invalid')
        (self.seed/'.gitignore').write_text('data/\ndownloads/\n')
        (self.seed/'VERSION').write_text('0.1\n')
        self.commit(self.seed,'initial')
        self.run_git(self.seed,'remote','add','origin',str(self.remote))
        self.run_git(self.seed,'push','-u','origin','main')
        self.run_git(self.temp,'clone',str(self.remote),str(self.checkout))
        self.run_git(self.checkout,'config','user.name','Test')
        self.run_git(self.checkout,'config','user.email','test@example.invalid')

    def tearDown(self):
        def writable_remove(function, path, unused):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(self.temp, onerror=writable_remove)

    def run_git(self,root,*args):
        result=subprocess.run(['git','-C',str(root)]+list(args),capture_output=True,text=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def commit(self,root,message):
        self.run_git(root,'add','VERSION','.gitignore')
        self.run_git(root,'-c','commit.gpgsign=false','commit','-m',message)

    def advance_remote(self):
        (self.seed/'VERSION').write_text('0.2\n')
        self.commit(self.seed,'next release');self.run_git(self.seed,'push')

    def test_update_preserves_machine_data_and_reaches_remote_head(self):
        (self.checkout/'data').mkdir();secret=self.checkout/'data'/'workspace.json';secret.write_text('keep local')
        (self.checkout/'downloads').mkdir();part=self.checkout/'downloads'/'large.part';part.write_bytes(b'keep partial')
        self.advance_remote()
        self.assertIn('已更新',launcher.update_checkout(self.checkout))
        self.assertEqual((self.checkout/'VERSION').read_text(),'0.2\n')
        self.assertEqual(secret.read_text(),'keep local');self.assertEqual(part.read_bytes(),b'keep partial')
        self.assertEqual(self.run_git(self.checkout,'rev-parse','HEAD'),self.run_git(self.checkout,'rev-parse','@{upstream}'))
        self.assertIn('已是远程最新版',launcher.update_checkout(self.checkout))

    def test_local_changes_are_not_overwritten(self):
        self.advance_remote();(self.checkout/'VERSION').write_text('my changes\n')
        with self.assertRaises(launcher.UpdateError):launcher.update_checkout(self.checkout)
        self.assertEqual((self.checkout/'VERSION').read_text(),'my changes\n')

    def test_unpushed_commits_and_divergence_stop_update(self):
        (self.checkout/'VERSION').write_text('my commit\n');self.commit(self.checkout,'local commit')
        before=self.run_git(self.checkout,'rev-parse','HEAD');self.advance_remote()
        with self.assertRaises(launcher.UpdateError):launcher.update_checkout(self.checkout)
        self.assertEqual(self.run_git(self.checkout,'rev-parse','HEAD'),before)
        self.assertEqual((self.checkout/'VERSION').read_text(),'my commit\n')

    def test_offline_fetch_is_reported_without_starting_old_code(self):
        self.run_git(self.checkout,'remote','set-url','origin',str(self.temp/'not-there.git'))
        before=self.run_git(self.checkout,'rev-parse','HEAD')
        with self.assertRaises(launcher.UpdateError):launcher.update_checkout(self.checkout)
        self.assertEqual(self.run_git(self.checkout,'rev-parse','HEAD'),before)


class PlatformTests(unittest.TestCase):
    def test_wsl_browser_uses_windows_with_loopback_url_only(self):
        url='http://127.0.0.1:12345/#token='+'a'*64
        with mock.patch.object(platform_support,'is_wsl',return_value=True),mock.patch.object(platform_support.shutil,'which',return_value='/mnt/c/Windows/powershell.exe'),mock.patch.object(platform_support.subprocess,'Popen') as popen:
            self.assertTrue(platform_support.open_workspace(url))
            self.assertEqual(popen.call_args.args[0][0],'powershell.exe')
            self.assertFalse(platform_support.open_workspace(url+"'; Write-Output secret"))
            self.assertEqual(popen.call_count,1)

    def test_wsl_download_folder_opens_in_windows_explorer(self):
        completed=subprocess.CompletedProcess([],0,'\\\\wsl.localhost\\Ubuntu\\home\\user\\downloads\n')
        with mock.patch.object(platform_support,'is_wsl',return_value=True),mock.patch.object(platform_support.shutil,'which',return_value='available'),mock.patch.object(platform_support.subprocess,'run',return_value=completed),mock.patch.object(platform_support.subprocess,'Popen') as popen:
            self.assertTrue(platform_support.reveal_local('/tmp/downloads/file.bin'))
            self.assertEqual(popen.call_args.args[0],['explorer.exe','\\\\wsl.localhost\\Ubuntu\\home\\user\\downloads'])


if __name__=='__main__':
    unittest.main(verbosity=2)
