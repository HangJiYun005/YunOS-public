import sqlite3, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from memory.sidecar_maintenance import _maintain_sidecar_transaction

class SidecarMaintenanceTest(unittest.TestCase):
 def test_empty_has_no_backup(self):
  with TemporaryDirectory() as d:
   p=Path(d)/'x.db'; sqlite3.connect(p).close()
   with patch('memory.sidecar_maintenance.validate_semantic_sidecar_write_target',return_value=p):
    r=_maintain_sidecar_transaction([],p,'abc'); self.assertEqual('no_action',r['status']); self.assertFalse(r['backup_created']); self.assertFalse(r['executed']); self.assertFalse(r['transaction_committed']); self.assertFalse(r['transaction_rolled_back']); self.assertEqual('abc',r['plan_id'])
 def test_busy_is_rejected(self):
  with TemporaryDirectory() as d:
   p=Path(d)/'x.db'; c=sqlite3.connect(p); c.execute('create table memory_embeddings (source_table,source_id,memory_space,source_platform,embedding_provider,embedding_model,model_revision,model_fingerprint,embedding_protocol,embedding_normalized,embedding_dim,embedding_json,content_hash,content_preview,metadata_json)'); c.execute('begin immediate')
   r={'source_table':'x','source_id':'1','memory_space':'x','source_platform':None,'embedding_provider':'x','embedding_model':'x','model_revision':'x','model_fingerprint':'x','embedding_protocol':'x','embedding_normalized':1,'embedding_dim':1,'embedding_json':'[]','content_hash':'x','content_preview':'','metadata_json':'{}'}
   with patch('memory.sidecar_maintenance.validate_semantic_sidecar_write_target',return_value=p), patch('memory.sidecar_maintenance.BACKUP_ROOT',Path(d)/'backups'): self.assertEqual('maintenance_busy',_maintain_sidecar_transaction([r],p,'abc')['status'])
   c.rollback(); c.close()
