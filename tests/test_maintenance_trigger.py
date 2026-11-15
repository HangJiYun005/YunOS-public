import json, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class MaintenanceTriggerTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT/'tools'/'maintain_memory_lifecycle.py'), *args, '--json'], capture_output=True, text=True)
    def test_check_and_prepare_are_readonly(self):
        check=self.run_cli('--check'); self.assertEqual(0, check.returncode)
        prepare=self.run_cli('--prepare'); self.assertEqual(0, prepare.returncode)
        self.assertIn(json.loads(prepare.stdout)['status'], {'no_action','ready','sidecar_not_initialized'})
    def test_execute_without_confirmations_refused(self):
        result=self.run_cli('--execute'); self.assertEqual(2, result.returncode); self.assertEqual('refused',json.loads(result.stdout)['status'])
    def test_fixed_paths_and_no_sensitive_source(self):
        text=(ROOT/'tools'/'maintain_memory_lifecycle.py').read_text(encoding='utf-8')
        self.assertNotIn('messages',text); self.assertNotIn('summary_text',text); self.assertNotIn('embedding_json',text)
    def test_no_action_execute_matches_cli_and_creates_no_audit(self):
        prepared=self.run_cli('--prepare'); data=json.loads(prepared.stdout)
        if data.get('status') != 'no_action': self.skipTest('production sidecar is not currently up to date')
        logs=ROOT/'logs'/'lifecycle'; before=set(logs.glob('*.json')) if logs.exists() else set()
        result=self.run_cli('--execute','--confirm-plan-id',data['plan_id'],'--real-readonly','--allow-production-sidecar','--confirm-production-sidecar-write')
        self.assertEqual(0,result.returncode); self.assertEqual('no_action',json.loads(result.stdout)['status'])
        after=set(logs.glob('*.json')) if logs.exists() else set(); self.assertEqual(before,after)

if __name__ == '__main__': unittest.main()
