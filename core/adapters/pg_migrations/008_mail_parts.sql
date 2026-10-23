-- 批次 17b：分片扫描。分片报告带 parent（整次扫描的 scan_id），按它查找；整次扫描的列表排除分片报告。
CREATE INDEX mail_runs_parent ON mail_runs ((report->>'parent')) WHERE report ? 'parent';
