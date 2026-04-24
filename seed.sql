-- =============================================================
--  TRMT3 Seed Data  —  감독·선박·매핑
--  (예시 이슈는 app.py init_db() 에서 Python 으로 주입 —
--   actions JSON 이스케이프 편의상)
-- =============================================================

-- -------------------------------------------------------------
--  감독 (임시 샘플 — 실제 명단 받으면 UPDATE)
-- -------------------------------------------------------------
INSERT OR IGNORE INTO supervisors (name, display_order, color, email) VALUES
    ('손차장', 1, 'blue',   'trmt3@sinokor.co.kr'),
    ('김과장', 2, 'teal',   ''),
    ('이과장', 3, 'purple', '');

-- -------------------------------------------------------------
--  선박
-- -------------------------------------------------------------
INSERT OR IGNORE INTO vessels (name, short_name, vessel_type, imo, class_society) VALUES
    ('KUWAIT PROSPERITY', 'KW PROSP',   'VLCC',      '9722936', 'BV'),
    ('KUWAIT GLORY',      'KW GLORY',   'VLCC',      '',        'BV'),
    ('SAUDI EXPORT',      'SA EXPORT',  'AFRAMAX',   '',        'BV'),
    ('ATLANTIC PIONEER',  'AT PIONEER', 'CONTAINER', '',        'BV');

-- -------------------------------------------------------------
--  감독-선박 담당 매핑
-- -------------------------------------------------------------
INSERT OR IGNORE INTO supervisor_vessels (supervisor_id, vessel_id)
SELECT s.id, v.id
  FROM supervisors s, vessels v
 WHERE (s.name = '손차장' AND v.name IN ('KUWAIT PROSPERITY', 'KUWAIT GLORY'))
    OR (s.name = '김과장' AND v.name = 'SAUDI EXPORT')
    OR (s.name = '이과장' AND v.name = 'ATLANTIC PIONEER');
