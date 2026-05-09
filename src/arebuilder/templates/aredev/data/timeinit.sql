-- Seed the minimal time row expected by the AREDev database schema. The
-- duplicate-key clause makes the script safe to rerun against an existing DB.
INSERT INTO `gs_system` (`row_key`, `value`) VALUES ('time', '1') ON DUPLICATE KEY UPDATE `value`='1';
