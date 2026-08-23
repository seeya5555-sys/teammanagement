"""감독 Daily DD report **PDF** 읽기 (형 지시 2026-08-23 "pdf도 읽어오기 가능하게").

`dock_daily_pdf.parse_pages()` 에 **좌표만** 먹여서 잠근다.  실제 PDF 를 fixture 로
두지 않는 이유가 둘이다.

1. 라이브 파일은 선박 운항·공사 내용이다 -- 저장소에 넣지 않는다.
2. PDF 를 만들려면 또 다른 라이브러리가 필요하고, 그러면 정작 잠그고 싶은 판정
   (컬럼 좌표·페이지 넘김·되풀이 머리글)이 그 라이브러리의 출력에 가려진다.

좌표는 라이브 파일 실측값이다: S/N 93-124 · Description 124-326 · Date start
326-399 · Scheduled finish 399-468 · (테두리 유령칸 468-473) · Date finish 473-546.
"""
import datetime as dt
import unittest

import dock_daily_docx as rules
import dock_daily_pdf as parser

D = dt.date(2026, 8, 22)

COLS = [(93, 124), (124, 326), (326, 399), (399, 468), (468, 473), (473, 546)]
NARROW = [(93, 124), (124, 326), (326, 399), (399, 473), (473, 546)]
HEADER = ['S/N', 'Description', 'Date start', 'Scheduled finish date', '', 'Date finish']


def word(text, x0, top, width=None):
    return {'text': text, 'x0': x0, 'x1': x0 + (width if width is not None else 6 * len(text)),
            'top': top, 'bottom': top + 10}


def cells_row(ranges, top, values, height=18):
    """한 행: 셀 좌표 + 각 칸 가운데에 놓은 글자."""
    row = {'bbox': (ranges[0][0], top, ranges[-1][1], top + height),
           'cells': [(x0, top, x1, top + height) for x0, x1 in ranges]}
    words = []
    for (x0, x1), text in zip(ranges, values):
        if text:
            words.append(word(text, (x0 + x1) / 2.0 - 3 * len(text), top + 4))
    return row, words


def table(ranges, top, rows, height=18):
    """`(표, 글자목록)`.  `rows` 는 칸 값의 목록."""
    out_rows, words = [], []
    for i, values in enumerate(rows):
        row, ws = cells_row(ranges, top + i * height, values, height)
        out_rows.append(row)
        words.extend(ws)
    return {'bbox': (ranges[0][0], top, ranges[-1][1], top + height * len(rows)),
            'rows': out_rows}, words


def page(*parts):
    """`parts` = `('para', top, text)` 또는 `('table', 표, 글자)`."""
    words, tables = [], []
    for part in parts:
        if part[0] == 'para':
            x = 93
            for token in part[2].split():
                words.append(word(token, x, part[1]))
                x += 6 * len(token) + 4
        else:
            tables.append(part[1])
            words.extend(part[2])
    return {'words': words, 'tables': tables}


DECK = 'Leading Deck Works done by the Yard'


def deck_page(rows, top=200):
    tbl, ws = table(COLS, top, [HEADER] + rows)
    return page(('para', top - 30, '3. ' + DECK), ('table', tbl, ws))


class ParseTest(unittest.TestCase):
    def test_header_row_maps_columns_by_x_position(self):
        rows = [['1.', 'Shifting vessel to dock', '22.08.2026', '', '', '22.08.2026'],
                ['2.', 'Overhaul of ME', '22.08.2026', '10.09.2026', '', '']]
        out = parser.parse_pages([deck_page(rows)])
        self.assertEqual([g['label'] for g in out['groups']], ['Deck (Yard)'])
        got = out['groups'][0]['rows']
        self.assertEqual([r['desc'] for r in got],
                         ['Shifting vessel to dock', 'Overhaul of ME'])
        self.assertEqual([(r['start'], r['sched'], r['finish']) for r in got],
                         [('22.08.2026', '', '22.08.2026'),
                          ('22.08.2026', '10.09.2026', '')])

    def test_row_key_matches_docx_path(self):
        """같은 작업은 형식이 달라도 같은 키다.

        🔴 안 그러면 감독이 같은 보고서를 Word 로 한 번, PDF 로 한 번 보낼 때 같은
        작업이 카드에 **두 줄**로 들어간다.
        """
        out = parser.parse_pages([deck_page([['1.', 'Docking of the vessel', '', '', '', '']])])
        self.assertEqual(out['groups'][0]['rows'][0]['row_key'],
                         rules.row_key('Deck (Yard)', 'Docking of the vessel'))

    def test_headerless_next_page_continues_the_table(self):
        """실측: Deck 표는 3쪽에서 7행까지, 4쪽에서 8행부터 **머리글 없이** 이어진다."""
        first = deck_page([['7.', 'Staging installation', '21.08.2026', '', '', '']])
        cont, ws = table(NARROW, 106, [['8.', 'Removing rope guard', '22.08.2026', '', '22.08.2026']])
        out = parser.parse_pages([first, page(('table', cont, ws))])
        self.assertEqual(len(out['groups']), 1)
        got = out['groups'][0]['rows']
        self.assertEqual([r['desc'] for r in got], ['Staging installation', 'Removing rope guard'])
        # 🔴 칸 수가 6→5 로 줄어든 쪽이다. 인덱스로 읽으면 완료일이 계획일 칸으로
        #    밀려 "금일 완료" 가 "계획 완료" 로 뒤집힌다.
        self.assertEqual((got[1]['sched'], got[1]['finish']), ('', '22.08.2026'))

    def test_cost_table_is_not_read_as_a_continuation(self):
        """`Description` 머리글이 있어도 컬럼 경계가 다르면 작업표가 아니다.

        실측 비용표는 `Description | Estimated (USD) | Actual` 3칸이고 Description 이
        93 에서 시작한다(작업표는 124).  겹침 비율로 보면 72% 라 통과해서, 예산 숫자가
        작업 카드로 들어간다.
        """
        first = deck_page([['1.', 'Staging installation', '21.08.2026', '', '', '']])
        cost, ws = table([(93, 269), (269, 410), (410, 546)], 400,
                         [['Description', 'Estimated (USD)', 'Actual'],
                          ['Docking fee', '10000', '9000']])
        out = parser.parse_pages([first, page(('table', cost, ws))])
        self.assertEqual(len(out['groups']), 1)
        self.assertEqual([r['desc'] for r in out['groups'][0]['rows']], ['Staging installation'])

    def test_page_break_fragment_joins_the_previous_row(self):
        """실측 6쪽 첫 행은 `stair tower hold access.` 하나뿐 -- 5쪽 마지막 문장의 뒷부분."""
        first = deck_page([['12.', 'Commence preparation for repair cell guides in hold',
                            '21.08.2026', '30.08.2026', '', '']])
        frag, ws = table(NARROW, 106, [['', 'stair tower hold access.', '', '', '']])
        out = parser.parse_pages([first, page(('table', frag, ws))])
        got = out['groups'][0]['rows']
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]['desc'].endswith('in hold stair tower hold access.'))
        # 키도 이어 붙인 문장으로 다시 만든다 -- 잘린 문장 키로 남기면 재읽기에서
        # 같은 작업이 새 카드로 또 들어온다.
        self.assertEqual(got[0]['row_key'], rules.row_key('Deck (Yard)', got[0]['desc']))

    def test_a_word_exactly_on_a_column_border_lands_in_one_column(self):
        """🔴 칸은 경계를 **공유**한다.  양쪽을 닫은 구간으로 읽으면 경계에 앉은 글자가
        두 컬럼에 동시에 들어가 날짜가 설명에 섞이거나 그 반대가 된다.

        라이브 파일에서는 경계 정확일치가 0건이었지만, 조건이 갖춰지면 조용히 틀리는
        종류라 구간 자체가 겹치지 않아야 한다(올마이트 지적 2026-08-23).
        """
        tbl, ws = table(COLS, 200, [HEADER, ['1.', 'Docking', '', '', '', '']])
        # 중심이 정확히 326.0 = Description.x1 = Date start.x0 인 글자
        ws.append({'text': '22.08.2026', 'x0': 321.0, 'x1': 331.0, 'top': 222, 'bottom': 232})
        out = parser.parse_pages([page(('para', 170, '3. ' + DECK), ('table', tbl, ws))])
        got = out['groups'][0]['rows'][0]
        hits = [name for name in ('desc', 'start') if '22.08.2026' in got[name]]
        self.assertEqual(len(hits), 1, '경계 글자가 두 컬럼에 동시에 들어갔다: %r' % (got,))
        self.assertEqual(got['desc'], 'Docking', '설명에 날짜가 섞이면 키까지 달라진다')

    def test_a_merged_first_row_does_not_lose_the_whole_continuation(self):
        """🔴 이어진 쪽의 **첫 행**에 병합으로 사라진 칸이 있으면, 첫 행만 보고 판정하던
        옛 규칙은 그 표를 통째로 버렸다 -- 실측 템플릿에 병합 행이 실제로 있다(1쪽).
        한 행이라도 좌표가 맞으면 같은 표의 연속이다.
        """
        first = deck_page([['7.', 'Staging installation', '21.08.2026', '', '', '']])
        cont, ws = table(NARROW, 106,
                         [['8.', 'Removing rope guard', '22.08.2026', '', '22.08.2026'],
                          ['9.', 'Docking of the vessel', '22.08.2026', '', '22.08.2026']])
        cont['rows'][0]['cells'] = [cont['rows'][0]['cells'][0], None, None, None, None]
        out = parser.parse_pages([first, page(('table', cont, ws))])
        self.assertEqual(len(out['groups']), 1)
        self.assertEqual([r['desc'] for r in out['groups'][0]['rows']],
                         ['Staging installation', 'Removing rope guard', 'Docking of the vessel'])

    def test_a_repeated_header_continues_the_table(self):
        """머리글을 되풀이하는 문서에서 표를 통째로 버리지 않는다.

        실측 템플릿은 되풀이하지 않지만(4·6쪽 다 머리글 없이 시작), 버리면 조용한
        손실이다.  좌표가 다 맞을 때만 잇는다 -- 비용표는 통과하지 못한다(위 테스트).
        """
        first = deck_page([['7.', 'Staging installation', '21.08.2026', '', '', '']])
        again, ws = table(COLS, 106, [HEADER,
                                      ['8.', 'Removing rope guard', '22.08.2026', '', '', '22.08.2026']])
        out = parser.parse_pages([first, page(('table', again, ws))])
        self.assertEqual(len(out['groups']), 1, '되풀이 머리글이 표를 버렸다')
        self.assertEqual([r['desc'] for r in out['groups'][0]['rows']],
                         ['Staging installation', 'Removing rope guard'])
        self.assertNotIn('Description', ' '.join(r['desc'] for r in out['groups'][0]['rows']),
                         '되풀이된 머리글 행이 작업으로 들어갔다')

    def test_a_numberless_row_mid_table_stays_its_own_row(self):
        """🔴 잘린 조각 붙이기는 이어진 표의 **첫 행**에만.  표 중간의 번호·날짜 없는
        행까지 앞 행에 붙이면 서로 무관한 두 작업이 한 문장으로 합쳐진다
        (올마이트 지적 2026-08-23).  붙이지 않아도 잃지는 않는다 -- 자기 행으로 남는다.
        """
        out = parser.parse_pages([deck_page([
            ['1.', 'Staging installation', '21.08.2026', '', '', ''],
            ['', 'Cleaning in ER', '', '', '', ''],
        ])])
        got = out['groups'][0]['rows']
        self.assertEqual([r['desc'] for r in got], ['Staging installation', 'Cleaning in ER'])

    def test_superscript_keeps_the_heading_on_one_line(self):
        """`3rd` 는 윗주라 top 이 0.6pt 높다.  줄을 top 으로 묶으면 제목이 쪼개진다."""
        words = []
        x = 93
        for token in '5. Leading Engine Works done by the'.split():
            words.append(word(token, x, 541.8))
            x += 6 * len(token) + 4
        words.append(word('3rd', x, 541.2))
        words.append(word('party', x + 22, 541.8))
        tbl, ws = table(COLS, 560, [HEADER, ['1.', 'ME TC Maintenance', '20.08.2026',
                                             '30.08.2026', '', '']])
        pg = {'words': words + ws, 'tables': [tbl]}
        out = parser.parse_pages([pg])
        self.assertEqual([(g['label'], g['section_key']) for g in out['groups']],
                         [('3rd party', 'vendor')])

    def test_word_outside_every_column_goes_to_the_nearest(self):
        """표 오른쪽 끝을 넘겨 그려진 글자를 버리지 않는다 -- 버리면 날짜가 사라진다."""
        tbl, ws = table(COLS, 200, [HEADER, ['1.', 'Docking of the vessel', '', '', '', '']])
        ws.append(word('22.08.2026', 550, 222))    # Date finish(-546) 오른쪽으로 넘침
        out = parser.parse_pages([page(('para', 170, '3. ' + DECK), ('table', tbl, ws))])
        got = out['groups'][0]['rows'][0]
        self.assertEqual((got['desc'], got['finish']), ('Docking of the vessel', '22.08.2026'))

    def test_unknown_heading_is_reported_not_dropped(self):
        tbl, ws = table(COLS, 200, [HEADER, ['1.', 'Diver inspection', '22.08.2026', '', '', '']])
        out = parser.parse_pages([page(('para', 170, '6. Leading Works done by Divers'),
                                      ('table', tbl, ws))])
        self.assertEqual(out['unmapped_headings'], ['6. Leading Works done by Divers'])
        self.assertIsNone(out['groups'][0]['section_key'])

    def test_table_without_heading_is_skipped(self):
        tbl, ws = table(COLS, 200, [HEADER, ['1.', 'Docking fee', '', '', '', '']])
        out = parser.parse_pages([page(('table', tbl, ws))])
        self.assertEqual(out['groups'], [])

    def test_empty_document_reports_nothing(self):
        """스캔 PDF(글자 없음)는 0건이다.  예외가 아니라 0건이어야 화면이 사유를 말한다."""
        out = parser.parse_pages([{'words': [], 'tables': []}])
        self.assertEqual((out['groups'], out['unmapped_headings']), ([], []))


class JudgeTest(unittest.TestCase):
    def test_verdicts_come_from_the_docx_rules(self):
        rows = [['1.', 'Docking of the vessel', '22.08.2026', '', '', '22.08.2026'],
                ['2.', 'Cleaning in ER', '20.08.2026', '', '', ''],
                ['3.', 'Overhaul of ME', '22.08.2026', '10.09.2026', '', '']]
        out = rules.judge(parser.parse_pages([deck_page(rows)]), D)
        got = out['groups'][0]['rows']
        self.assertEqual([(r['verdict'], r['reason']) for r in got],
                         [('include', 'finished_today'), ('exclude', 'past_one_off'),
                          ('include', 'started_today')])
        self.assertEqual([r['marker'] for r in got], ['금일 완료', '', '계획 완료 09.10'])
        self.assertEqual(out['counts'], {'include': 2, 'exclude': 1, 'unknown': 0})


class HeadingRuleTest(unittest.TestCase):
    def test_both_templates_map_to_the_same_labels(self):
        """PDF 템플릿(TDF-04.7c)은 `works` 앞에 `Engine` 을 끼운다(실측).

        🔴 라벨은 두 템플릿에서 **같아야** 한다 -- `row_key` 의 scope 라서, 달라지면
        이미 적용한 카드가 다음 읽기에서 새 줄로 또 들어온다.
        """
        cases = [('4. Leading Deck Works done by the Yard', ('shipyard', 'Deck (Yard)')),
                 ('4. Leading Engine Works done by the Yard', ('shipyard', 'Engine (Yard)')),
                 ('5. Leading Works done by 3rd party', ('vendor', '3rd party')),
                 ('5. Leading Engine Works done by the 3rd party', ('vendor', '3rd party')),
                 ('6. Leading Works done by Crew', ('crew', 'Crew')),
                 ('6. Leading Engine Works done by the Crew', ('crew', 'Crew'))]
        for text, expect in cases:
            self.assertEqual(rules.heading_target(text), expect, text)


if __name__ == '__main__':
    unittest.main()
