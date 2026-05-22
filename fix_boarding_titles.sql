-- 기존 Boarding Report 섹션 제목에서 자동 번호 prefix 제거
-- (신규 보고서는 코드 수정으로 이미 처리되므로, 기존 데이터만 정리)
UPDATE boarding_report_sections
   SET title = TRIM(REPLACE(REPLACE(REPLACE(REPLACE(title,
                  '1. ', ''), '2. ', ''), '3. ', ''), '4. ', ''))
 WHERE title LIKE '1. %'
    OR title LIKE '2. %'
    OR title LIKE '3. %'
    OR title LIKE '4. %';
