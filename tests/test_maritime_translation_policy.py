"""Exercise real prompt builders/callers; no API, DB write, or receipt OCR changes."""
import unittest
from unittest.mock import patch
import helpers_shared as h
import ai_gemini as a
from maritime_style import MARITIME_TERMS_RULES as RULES, EN_TRANSLATION_RULES

class MaritimeTranslationPolicyTests(unittest.TestCase):
    def test_survey_prompt_paths_and_original_contracts(self):
        prompts = [a._findings_prompt('cs'), a._findings_prompt('vt'),
                   h._class_status_prompt(), a._vetting_full_prompt(),
                   a._full_report_prompt({'report_number':'SYNTHETIC'}, []),
                   a._close_prompt({'report_number':'SYNTHETIC','inspection_date':'2026-01-01'}, 'TEST', [], 'synthetic'),
                   h._dd_ko_prompt('[]')]
        for prompt in prompts:
            with self.subTest(prompt=prompt[:80]):
                self.assertIn(RULES, prompt)
                self.assertIn('Spare 보급', prompt)
                self.assertIn('한국어 번역·요약 필드에만', prompt)
        self.assertIn('원문 그대로', a._findings_prompt('cs'))
        self.assertIn('due_date', h._class_status_prompt())
        self.assertIn('EXACT verbatim', prompts[5])
        self.assertIn('"status":"Closed"', prompts[5])

    def test_daily_summary_call_and_identity(self):
        seen=[]
        def call(parts, **kw):
            seen.append(parts[0]['text'])
            return {'items':[{'i':7,'desc':'Compressor 고장','action':'Spare 보급 대기 중'}]}
        with patch.object(h,'GEMINI_API_KEY','synthetic'),patch.object(h,'_gemini_call_json',call):
            out=h._gen_issue_summaries([{'i':7,'description':'Compressor failed','action':'Awaiting spare'}])
        self.assertIn(RULES,seen[0]);self.assertIn(7,out)

    def test_excel_direct_mapping_summaries_use_policy(self):
        seen=[]
        def call(parts, **kw):
            seen.append(parts[0]['text']);return {'summaries':[{'i':0,'remark':'Wire rope 마모'}]}
        with patch.object(a,'GEMINI_API_KEY','synthetic'),patch.object(a,'_gemini_call_json',call):
            a._summarize_remarks([{'description':'Wire rope worn'}],'cs')
        self.assertIn(RULES,seen[0])

    def test_en_translation_not_forced_to_korean(self):
        seen=[]
        def call(parts, **kw):
            seen.append(parts[0]['text']);return {'translations':[{'i':0,'en':'Supply Spare'}]}
        with patch.object(h,'_gemini_call_json',call):
            out=h._translate_batch_en(['Spare 보급'],[0])
        self.assertEqual({0:'Supply Spare'},out)
        self.assertIn(EN_TRANSLATION_RULES,seen[0]);self.assertNotIn(RULES,seen[0])

    def test_ko_batch_identity_and_failure(self):
        with patch.object(h,'_gemini_call_json',return_value={'translations':[{'i':2,'ko':'Spare 보급 대기 중'}]}):
            self.assertEqual({2:'Spare 보급 대기 중'},h._translate_batch_ko(['','','Awaiting spares'],[2]))
        with patch.object(h,'_gemini_call_json',return_value={'error':'API_CALL_FAILED'}):
            self.assertIsNone(h._translate_batch_ko(['Awaiting spares'],[0]))

    def test_third_sentence_deadline_not_lost(self):
        source='Compressor 임시수리 완료. Pressure test 예정. Spare 보급 후 영구수리 필요, 기한 2027-03-31.'
        out=a._concise_full_report_remark(source)
        self.assertIn('Spare 보급 후 영구수리 필요',out)
        self.assertIn('2027-03-31',out)
        self.assertNotIn('…',out)

    def test_second_sentence_without_status_keyword_preserved(self):
        out=a._concise_full_report_remark('Compressor 임시수리 완료. Spare 미보급.')
        self.assertIn('Spare 미보급',out)

if __name__=='__main__':unittest.main()
