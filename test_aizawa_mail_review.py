#!/usr/bin/env python3
"""Aizawa 현안 메일 검토 큐의 API 계약 테스트 (외부 Outlook/LLM 호출 없음)."""
import importlib
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(__file__)
sys.path.insert(0, ROOT)
appmod = importlib.import_module('app')


def setup_database(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE api_settings (k TEXT PRIMARY KEY, v TEXT);
        INSERT INTO api_settings(k, v) VALUES ('api_key', 'test-key');
        CREATE TABLE mail_card (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_subject TEXT,
            email_from TEXT,
            email_date TEXT,
            email_msg_id TEXT,
            thread_key TEXT,
            thread_summary_ko TEXT,
            summary_ko TEXT,
            body_en TEXT,
            action_summary TEXT,
            issue_item TEXT,
            issue_desc TEXT,
            issue_match_id INTEGER,
            issue_vessel TEXT,
            issue_supervisor TEXT,
            issue_priority TEXT,
            card_category TEXT,
            issue_status TEXT NOT NULL DEFAULT 'pending',
            reply_status TEXT NOT NULL DEFAULT 'none',
            card_status TEXT NOT NULL DEFAULT 'active',
            pending INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE shipwiki_card (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            card_type TEXT,
            title TEXT,
            body TEXT
        );
        INSERT INTO mail_card
          (email_subject, email_from, email_date, email_msg_id, thread_summary_ko,
           summary_ko, body_en, issue_item, issue_desc, issue_match_id, issue_vessel,
           issue_priority, card_status, pending)
        VALUES
          ('[MT TEST] BWTS alarm', 'manager@example.com', '2026-07-12T09:00:00', 'outlook-1',
           'Same alarm continues.', '기존 자동 후보 요약', 'Original mail body',
           'BWTS alarm', 'Existing auto candidate', 99, 'MT TEST', 'Urgent', 'active', 0),
          ('[MT TEST] pending', 'manager@example.com', '2026-07-12T10:00:00', 'outlook-2',
           '', '', 'pending body', '', '', NULL, 'MT TEST', 'Normal', 'active', 1),
          ('[MT TEST] archived', 'manager@example.com', '2026-07-12T11:00:00', 'outlook-3',
           '', '', 'archived body', '', '', NULL, 'MT TEST', 'Normal', 'archived', 0);
    """)
    conn.commit()
    conn.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, 'trmt.db')
        setup_database(db)
        appmod.app.config.update(TESTING=True, DATABASE=db)
        client = appmod.app.test_client()
        headers = {'X-API-Key': 'test-key'}

        queued = client.get('/api/ext/mail/review-queue', headers=headers)
        assert queued.status_code == 200, queued.get_data(as_text=True)
        queue = queued.get_json()['queue']
        assert len(queue) == 1 and queue[0]['card_id'] == 1
        assert queue[0]['auto_candidate']['item'] == 'BWTS alarm'

        invalid = client.post('/api/ext/mail/reviews', headers=headers,
                              json={'card_id': 1, 'review': {'headline': 'only headline'}})
        assert invalid.status_code == 400, invalid.get_data(as_text=True)
        bad_enum = {'headline': 'h', 'assessment': 'a', 'evidence': ['e'], 'recommended_actions': ['r'],
                    'issue_candidate': {'recommendation': 'auto_execute'}, 'questions': ['q']}
        assert client.post('/api/ext/mail/reviews', headers=headers,
                           json={'card_id': 1, 'review': bad_enum}).status_code == 400
        assert client.post('/api/ext/mail/reviews', headers=headers,
                           json={'card_id': True, 'review': bad_enum}).status_code == 400
        missing = client.post('/api/ext/mail/reviews', headers=headers,
                              json={'card_id': 999, 'review': {}})
        assert missing.status_code == 400, missing.get_data(as_text=True)

        review = {
            'headline': 'BWTS alarm: confirm maker response deadline',
            'assessment': '사람 검토용 판단 초안. Daily 반영·메일 회신·상태 변경은 하지 않음.',
            'evidence': ['Original mail says alarm remains active.'],
            'recommended_actions': ['Maker reply ETA 확인', '운항 영향 여부 확인'],
            'issue_candidate': {'recommendation': 'append', 'reason': '기존 issue #99 연속 건', 'target_issue_id': 99},
            'questions': ['선박 현장 조치 완료 여부 확인 필요'],
        }
        assert client.post('/api/ext/mail/reviews', headers=headers,
                           json={'card_id': 2, 'review': review}).status_code == 400
        assert client.post('/api/ext/mail/reviews', headers=headers,
                           json={'card_id': 3, 'review': review}).status_code == 400
        saved = client.post('/api/ext/mail/reviews', headers=headers,
                            json={'card_id': 1, 'review': review, 'reviewer': 'aizawa'})
        assert saved.status_code == 201, saved.get_data(as_text=True)
        assert saved.get_json()['status'] == 'completed'

        # 결과 적재는 review 테이블만 바꿔야 한다. 기존 자동 후보·카드 상태는 승인 전 그대로다.
        with appmod.app.app_context():
            source = appmod.query('SELECT issue_status, issue_item, issue_desc, card_status, pending FROM mail_card WHERE id=1', one=True)
            assert source['issue_item'] == 'BWTS alarm'
            assert source['issue_desc'] == 'Existing auto candidate'
            assert source['card_status'] == 'active' and source['pending'] == 0

        # completed 결과는 동일 POST 재전송으로 덮어쓰지 않는다 (재전달 멱등성).
        changed = dict(review, headline='should not overwrite')
        again = client.post('/api/ext/mail/reviews', headers=headers,
                            json={'card_id': 1, 'review': changed, 'reviewer': 'retry'})
        assert again.status_code == 201

        # 완료된 카드는 재실행 큐에 노출되지 않아야 한다.
        empty = client.get('/api/ext/mail/review-queue', headers=headers)
        assert empty.status_code == 200
        assert empty.get_json()['queue'] == []

        with appmod.app.app_context():
            row = appmod.query('SELECT status, reviewer, review_json FROM aizawa_mail_review WHERE card_id=1', one=True)
            assert row['status'] == 'completed'
            assert row['reviewer'] == 'aizawa'
            assert 'Daily 반영' in row['review_json']
            assert 'should not overwrite' not in row['review_json']

        # 카드 적재 범주는 첨부된 고정 목록만 허용하고, 정상 payload는 DB·목록 응답에 보존한다.
        bad_category = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-bad-category', 'outlook_categories': ['현안'],
            'card_category': '기술-Next DD',
        })
        assert bad_category.status_code == 400, bad_category.get_data(as_text=True)
        created = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-category-aor', 'email_subject': 'AOR approval request',
            'thread_key': 'thread-category-aor',
            'outlook_categories': ['현안'], 'card_category': 'AOR',
        })
        assert created.status_code == 201, created.get_data(as_text=True)
        category_card_id = created.get_json()['id']
        # 구 runner가 범주를 아직 전송하지 않아도 기존 pending 카드의 확정 범주를 덮어쓰면 안 된다.
        legacy_update = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-category-aor-retry', 'email_subject': 'legacy runner retry',
            'thread_key': 'thread-category-aor',
            'outlook_categories': ['현안'],
        })
        assert legacy_update.status_code == 200, legacy_update.get_data(as_text=True)
        default_insert = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-default-category', 'outlook_categories': ['현안'],
        })
        assert default_insert.status_code == 201, default_insert.get_data(as_text=True)
        default_card_id = default_insert.get_json()['id']
        explicit_null = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-null-category', 'outlook_categories': ['현안'],
            'card_category': None,
        })
        assert explicit_null.status_code == 201, explicit_null.get_data(as_text=True)
        with appmod.app.app_context():
            default_row = appmod.query('SELECT card_category FROM mail_card WHERE id=?', (default_card_id,), one=True)
            assert default_row['card_category'] == '기술-Normal'
        for n, category in enumerate(('SIRE', '기술-COC&Flag', '기술-Normal', '기술-Urgent'), start=1):
            accepted = client.post('/api/ext/mail/cards', headers=headers, json={
                'email_msg_id': f'outlook-category-{n}', 'outlook_categories': ['현안'],
                'card_category': category,
            })
            assert accepted.status_code == 201, accepted.get_data(as_text=True)
        non_string = client.post('/api/ext/mail/cards', headers=headers, json={
            'email_msg_id': 'outlook-list-category', 'outlook_categories': ['현안'],
            'card_category': ['AOR'],
        })
        assert non_string.status_code == 400, non_string.get_data(as_text=True)
        with appmod.app.app_context():
            row = appmod.query('SELECT card_category FROM mail_card WHERE id=?', (category_card_id,), one=True)
            assert row['card_category'] == 'AOR'
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
        listed = client.get('/api/mail/cards')
        assert listed.status_code == 200
        assert any(c['id'] == category_card_id and c['card_category'] == 'AOR'
                   for c in listed.get_json()['cards'])

    print('PASS: aizawa mail review queue contract')


if __name__ == '__main__':
    main()
